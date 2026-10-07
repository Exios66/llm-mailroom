"""Gmail intake channel — the mailroom agent mailbox as a second intake route.

The agent mailbox (default ``llmmailroom@gmail.com``, a Gmail account with a
2FA app password) is another way documents enter the mailroom. This poller
fetches unseen messages over IMAP SSL (stdlib ``imaplib`` — no new
dependencies) and saves processable attachments into the SAME inbox bin the
watcher drains, writing the SAME ``<file>.meta`` sidecar the ``/upload``
endpoint writes. Nothing downstream changes: inbox →
``processing/<worker_id>/`` → archive/review/failed.

Watcher alignment: the poller runs INSIDE the watcher process (both the
API-embedded watcher and ``python -m pipeline.watcher`` — one ``watcher.lock``
drain point), started by ``Watcher.start()`` when enabled; it can also run
standalone via ``python -m pipeline.gmail_intake`` (debug/ops).

Credentials live ONLY in the gitignored ``.env``:

    MAILROOM_GMAIL_ENABLED=1
    GMAIL_ADDRESS=llmmailroom@gmail.com
    GMAIL_APP_PASSWORD=<16-char app password>   # spaces tolerated, stripped

Routing and guards:

- ``matter_id`` comes from a ``[M:<matter_id>]`` tag in the subject
  (e.g. ``Invoice scan [M:Smith-001]``) or falls back to
  ``MAILROOM_GMAIL_DEFAULT_MATTER_ID`` (default ``DEFAULT``).
- Only attachments whose extension is in ``accepted_extensions()`` queue; the
  ``.meta`` sidecar never matches and is never claimed.
- Attachment size is capped (``MAILROOM_GMAIL_MAX_ATTACHMENT_MB``, default 50).
- Optional sender allowlist (``MAILROOM_GMAIL_ALLOWED_SENDERS`` csv; empty =
  accept all). An allowlisted sender must also carry Gmail's own
  ``dmarc=pass`` verdict (``MAILROOM_GMAIL_REQUIRE_DMARC``, default on), and
  automated mail (auto-replies, bounces, lists, our own address) is skipped
  without a reply (``pipeline/mail_guards.py``).
- **Single vs bundle routing (HUB-037)**: an email carrying exactly ONE
  accepted attachment is a *single document upload* and each sidecar records
  ``route: triage`` — the watcher then dispatches it to the free-triage lane
  (triage team performs the core pipeline steps: deterministic prep → triage
  classification → auditable-hash archive with its own ``triage_*`` audit
  section → completion echo; no paid pipeline agents). An email carrying TWO
  OR MORE accepted attachments is a *multi-document upload*: ``route:
  pipeline``, the triage approach is dropped, and every attachment runs the
  FULL paid pipeline.
- Handled messages are marked ``\\Seen``; the ``Message-ID`` header is recorded
  in a bounded state file (``<base>/gmail_intake_state.json``) so a seen-mark
  failure can never double-queue an email.
"""

from __future__ import annotations

import collections
import datetime
import email
import email.utils
import errno
import imaplib
import json
import os
import re
import select
import shutil
import smtplib
import socket
import ssl
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from . import mail_guards
from .env import load_env

logger = structlog.get_logger(__name__)

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_FOLDER = "INBOX"
DEFAULT_POLL_SECONDS = 60.0
DEFAULT_MAX_ATTACHMENT_MB = 50
IMAP_TIMEOUT_SECONDS = 30
_STATE_KEEP_MESSAGE_IDS = 2000
_STATE_KEEP_FAILED_ATTEMPTS = 2000
# A message whose handling keeps raising is quarantined (marked \\Seen, labelled,
# recorded as processed) after this many failed sweeps instead of being
# retried forever. Override: MAILROOM_GMAIL_MAX_ATTEMPTS.
MAX_MESSAGE_ATTEMPTS = 3
FAILED_LABEL = "mailroom/failed"
_MAX_FILENAME_CHARS = 120
_MAX_FILENAME_BYTES = 200
_ORPHAN_MAX_AGE_SECONDS = 3600
# Filesystems without hard links / cross-device links: fall back to the copy path.
_LINK_FALLBACK_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EXDEV", "EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "ENOSYS")
    if hasattr(errno, name)
)
# The "check" reaction: an emoji-named Gmail label applied to the source
# message when the watcher picks the attachment up for processing (HUB-037).
DEFAULT_REACTION_LABEL = "✅"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465

# Matter routing: subject tag ``[M:<matter_id>]`` (watcher files the document
# under this matter via the inbox meta sidecar).
_MATTER_TAG_RE = re.compile(r"\[M:([A-Za-z0-9_.-]{1,64})\]")

_FILENAME_UNSAFE_RE = re.compile(r"[\x00-\x1f/\\]")
_DOT_RUN_RE = re.compile(r"\.{2,}")
_STAGE_EXT_RE = re.compile(r"\.[a-z0-9]{1,10}")

# Maximum size for the reaction/echo dedup sets to prevent unbounded memory
# growth in long-running watcher processes.
_BOUNDED_SET_MAX = 5000


class _BoundedSet:
    """A set that evicts the oldest entry when it exceeds ``max_size``.

    Uses an ``OrderedDict`` internally for O(1) membership testing with
    insertion-order eviction.  Thread-safe via the caller's existing lock.
    """

    def __init__(self, max_size: int = _BOUNDED_SET_MAX):
        self._data: collections.OrderedDict = collections.OrderedDict()
        self._max_size = max_size

    def __contains__(self, key) -> bool:
        return key in self._data

    def add(self, key) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        else:
            self._data[key] = None
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def discard(self, key) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class GmailIntakeError(RuntimeError):
    """Raised for fatal IMAP protocol failures inside a sweep."""

_STATUS_LOCK = threading.Lock()
_STATUS: dict = {
    "enabled": False,
    "running": False,
    "last_poll_at": None,
    "last_error": None,
    "messages_seen": 0,
    "attachments_queued": 0,
    "reactions_sent": 0,
    "reactions_failed": 0,
    "echoes_sent": 0,
}

# Test/smoke seam: when set, EVERY SMTP access in this module (echo emails)
# goes through this factory instead of a real smtplib connection.
_INJECTED_SMTP_FACTORY = None


def set_smtp_factory(factory) -> None:
    """Inject an SMTP client factory (test/smoke seam). Pass None to reset."""
    global _INJECTED_SMTP_FACTORY
    _INJECTED_SMTP_FACTORY = factory


def _record_status(**updates) -> None:
    with _STATUS_LOCK:
        _STATUS.update(updates)


def status() -> dict:
    """Snapshot of the Gmail intake channel state (safe for /health)."""
    with _STATUS_LOCK:
        return dict(_STATUS)


def gmail_intake_enabled() -> bool:
    """Whether the Gmail channel is configured on.

    Requires an explicit opt-in (``MAILROOM_GMAIL_ENABLED=1``) AND both
    credentials present. Never enabled silently — an account misconfigured
    mid-flight must not start network polling on its own.
    """
    load_env()
    if str(os.environ.get("MAILROOM_GMAIL_ENABLED", "")).strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    return bool(gmail_address() and gmail_app_password())


def gmail_address() -> str:
    return str(os.environ.get("GMAIL_ADDRESS", "")).strip()


def gmail_app_password() -> str:
    """App password with the display spaces stripped (Gmail shows them grouped)."""
    return str(os.environ.get("GMAIL_APP_PASSWORD", "")).replace(" ", "").strip()


def reactions_enabled() -> bool:
    """Whether the watcher should react to source emails (default on)."""
    load_env()
    return str(os.environ.get("MAILROOM_GMAIL_REACTIONS", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def reaction_label() -> str:
    """The emoji label applied as the reaction (default ✅)."""
    load_env()
    label = os.environ.get("MAILROOM_GMAIL_REACTION_LABEL", "").strip()
    return label or DEFAULT_REACTION_LABEL


# One reaction per message even when an email carried several attachments —
# the label is idempotent, but each claim would otherwise open its own IMAP
# connection for the same Message-ID.  Bounded to prevent memory growth in
# long-running watcher processes (oldest entry evicted at capacity).
_REACTION_ATTEMPTED: _BoundedSet = _BoundedSet()
_REACTION_LOCK = threading.Lock()


def _to_mutf7(label: str) -> bytes:
    """RFC 3501 modified-UTF-7 encoding for IMAP label/mailbox names.

    Gmail over IMAP cannot take non-ASCII label bytes in quoted strings (no
    UTF8=ACCEPT support — the server answers ``BAD Could not parse command``)
    and rejects literals in the X-GM-LABELS position. mUTF-7 is pure ASCII on
    the wire and Gmail decodes it back to the emoji label in the UI
    (``✅`` → ``&JwU-`` — verified live).
    """
    import base64

    out = bytearray()
    buf = ""

    def _flush():
        nonlocal buf
        if buf:
            out.extend(
                b"&" + base64.b64encode(buf.encode("utf-16-be")).rstrip(b"=") + b"-"
            )
            buf = ""

    for ch in label:
        if ord(ch) < 0x80:
            _flush()
            out += ch.encode("ascii")
        else:
            buf += ch
    _flush()
    return bytes(out)


def react_to_message(
    message_id: str,
    *,
    config: dict | None = None,
    imap_factory=None,
) -> bool:
    """React to the source email with the check emoji (a Gmail label).

    Applied by the WATCHER at claim time — the moment the document has hit
    the inbox and been picked up for processing. Never raises: a reaction
    failure must not disturb the document path (logged, retried on the next
    claim of the same message). Returns True when the label was applied.
    """
    if not message_id:
        return False
    with _REACTION_LOCK:
        if message_id in _REACTION_ATTEMPTED:
            return True
        _REACTION_ATTEMPTED.add(message_id)

    cfg = config or load_config()
    label = reaction_label()
    ok = False
    error_detail = ""
    client = None
    try:
        factory = (
            imap_factory
            or _INJECTED_IMAP_FACTORY
            or (lambda: imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=IMAP_TIMEOUT_SECONDS))
        )
        client = factory()
        client.login(cfg["address"], cfg["password"])
        typ, _ = client.select(cfg["folder"], readonly=False)
        if typ != "OK":
            raise GmailIntakeError(f"cannot select folder {cfg['folder']!r}")
        wire_label = b'("' + _to_mutf7(label) + b'")'
        # Best-effort label creation (Gmail auto-creates on STORE in most
        # cases; CREATE makes it deterministic). Already-exists errors ignored.
        try:
            client.create(b'"' + _to_mutf7(label) + b'"')
        except Exception:
            pass
        # RFC 3501 search by Message-ID (verified live against Gmail).
        typ, data = client.uid(
            "SEARCH", None, f'HEADER Message-ID "{message_id}"'.encode("utf-8")
        )
        uids = (data[0] or b"").split() if typ == "OK" and data else []
        for uid in uids:
            typ, _ = client.uid(
                "STORE",
                uid,
                "+X-GM-LABELS",
                wire_label,
            )
            if typ == "OK":
                ok = True
                logger.info("gmail_reaction_applied", message_id=message_id, label=label)
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "gmail_reaction_failed",
            message_id=message_id,
            label=label,
            error=error_detail,
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

    if ok:
        _record_status(reactions_sent=_STATUS["reactions_sent"] + 1)
    else:
        _record_status(reactions_failed=_STATUS.get("reactions_failed", 0) + 1)
        # Allow a later claim of the same message to retry the reaction.
        with _REACTION_LOCK:
            _REACTION_ATTEMPTED.discard(message_id)
    return ok


# Test/smoke seam: when set, EVERY IMAP access in this module (poll sweeps and
# reactions) goes through this factory instead of a real imaplib connection —
# network-free evidence without env surgery.
_INJECTED_IMAP_FACTORY = None


def set_imap_factory(factory) -> None:
    """Inject an IMAP client factory (test/smoke seam). Pass None to reset."""
    global _INJECTED_IMAP_FACTORY
    _INJECTED_IMAP_FACTORY = factory


def load_config() -> dict:
    """Effective channel configuration from the environment."""
    load_env()
    try:
        max_mb = int(os.environ.get("MAILROOM_GMAIL_MAX_ATTACHMENT_MB", DEFAULT_MAX_ATTACHMENT_MB))
    except ValueError:
        max_mb = DEFAULT_MAX_ATTACHMENT_MB
    try:
        poll_seconds = float(os.environ.get("MAILROOM_GMAIL_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    except ValueError:
        poll_seconds = DEFAULT_POLL_SECONDS
    senders_raw = os.environ.get("MAILROOM_GMAIL_ALLOWED_SENDERS", "")
    return {
        "address": gmail_address(),
        "password": gmail_app_password(),
        "imap_host": os.environ.get("MAILROOM_GMAIL_IMAP_HOST", DEFAULT_IMAP_HOST),
        "imap_port": int(os.environ.get("MAILROOM_GMAIL_IMAP_PORT", DEFAULT_IMAP_PORT)),
        "smtp_host": os.environ.get("MAILROOM_GMAIL_SMTP_HOST", DEFAULT_SMTP_HOST),
        "smtp_port": int(os.environ.get("MAILROOM_GMAIL_SMTP_PORT", DEFAULT_SMTP_PORT)),
        "folder": os.environ.get("MAILROOM_GMAIL_FOLDER", DEFAULT_FOLDER),
        "poll_seconds": max(1.0, poll_seconds),
        "default_matter_id": os.environ.get("MAILROOM_GMAIL_DEFAULT_MATTER_ID", "DEFAULT"),
        "allowed_senders": {
            part.strip().lower() for part in senders_raw.split(",") if part.strip()
        },
        "max_attachment_bytes": max(1, max_mb) * 1024 * 1024,
        "require_dmarc": mail_guards.require_dmarc(),
        "max_replies_per_hour": mail_guards.max_replies_per_hour(),
    }


def parse_matter_id(subject: str | None) -> str | None:
    """Extract the ``[M:<matter_id>]`` subject tag, or None."""
    if not subject:
        return None
    match = _MATTER_TAG_RE.search(subject)
    if match is None:
        return None
    return match.group(1)


def _state_path():
    from .bins import get_base_dir

    return get_base_dir() / "gmail_intake_state.json"


def _max_attempts() -> int:
    try:
        value = int(os.environ.get("MAILROOM_GMAIL_MAX_ATTEMPTS", MAX_MESSAGE_ATTEMPTS))
    except (TypeError, ValueError):
        return MAX_MESSAGE_ATTEMPTS
    return value if value >= 1 else MAX_MESSAGE_ATTEMPTS


def _load_state() -> dict:
    state: dict = {"processed_message_ids": [], "failed_attempts": {}}
    try:
        data = json.loads(_state_path().read_text())
        if isinstance(data, dict):
            ids = data.get("processed_message_ids", [])
            state["processed_message_ids"] = [str(i) for i in ids if i]
            failed = data.get("failed_attempts", {})
            if isinstance(failed, dict):
                state["failed_attempts"] = {
                    str(k): int(v)
                    for k, v in failed.items()
                    if isinstance(v, int) and not isinstance(v, bool)
                }
    except FileNotFoundError:
        pass
    except Exception:
        logger.error("gmail_intake_state_unreadable", path=str(_state_path()), exc_info=True)
    return state


def _save_state(state: dict) -> None:
    try:
        ids = state.get("processed_message_ids", [])[-_STATE_KEEP_MESSAGE_IDS:]
        failed = dict(list(state.get("failed_attempts", {}).items())[-_STATE_KEEP_FAILED_ATTEMPTS:])
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            {
                "processed_message_ids": ids,
                "failed_attempts": failed,
                "updated_at": _now_iso(),
            }
        )
        try:
            with open(tmp, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except Exception:
        logger.exception("gmail_intake_state_write_failed")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = _FILENAME_UNSAFE_RE.sub("_", name)
    name = _DOT_RUN_RE.sub(".", name).strip(" .")
    if len(name) > _MAX_FILENAME_CHARS or len(name.encode("utf-8")) > _MAX_FILENAME_BYTES:
        dot = name.rfind(".")
        suffix = name[dot:] if 0 < dot and len(name) - dot <= 16 else ""
        if len(suffix.encode("utf-8")) > 40:
            suffix = ""
        stem = name[: len(name) - len(suffix)][: _MAX_FILENAME_CHARS - len(suffix)]
        budget = _MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
        name = stem + suffix if stem else ""
    return name or None


def _staging_dir() -> Path:
    from .bins import inbox_dir

    inbox = inbox_dir()
    return inbox.with_name(inbox.name + ".staging")


def _stage_path(ext: str) -> Path:
    """Create an empty same-filesystem staging file and return its path.

    Lives next to the inbox (``<inbox>.staging``) so the move into the inbox
    is a same-device link/rename, and always ends in ``.part`` so the watcher
    (which claims only accepted extensions) can never pick it up.
    """
    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    ext = ext.lower() if _STAGE_EXT_RE.fullmatch(ext.lower() if ext else "") else ""
    # os.open with 0o666 so the umask governs the mode (NamedTemporaryFile
    # would force 0o600, unreadable by a watcher running as another user).
    while True:
        path = staging / f"gmail-{uuid.uuid4().hex[:12]}{ext}.part"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666))
            return path
        except FileExistsError:
            continue


def _sweep_orphans() -> None:
    """Best-effort removal of stale ``.part`` files left by hard crashes."""
    try:
        from .bins import inbox_dir

        cutoff = time.time() - _ORPHAN_MAX_AGE_SECONDS
        staging = _staging_dir()
        candidates = []
        if staging.is_dir():
            candidates += list(staging.glob("*.part"))
        inbox = inbox_dir()
        if inbox.is_dir():
            candidates += list(inbox.glob(".*.part"))
        for path in candidates:
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        logger.exception("gmail_orphan_sweep_failed")


def _write_sidecar(dest: Path, meta: dict) -> Path:
    """Create ``<dest>.meta`` atomically and exclusively, fsynced.

    Raises ``FileExistsError`` when a sidecar already exists: the watcher reads
    the sidecar AFTER claiming its file, so replacing one could hand another
    document's intake metadata (matter, sender, route) to a pending claim.
    """
    from .bins import inbox_meta_path

    side = inbox_meta_path(dest)
    tmp = dest.parent / f".{uuid.uuid4().hex}.meta.part"
    try:
        with open(tmp, "w") as fh:
            fh.write(json.dumps(meta, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, side)  # fails if the sidecar exists
        except FileExistsError:
            raise
        except OSError:
            # No hard-link support: check-then-rename (sole-writer names).
            if os.path.lexists(side):
                raise FileExistsError(str(side)) from None
            os.rename(tmp, side)
    finally:
        tmp.unlink(missing_ok=True)
    return side


def _publish_no_clobber(publish, dest_dir: Path, name: str, meta: dict | None) -> Path:
    """Run ``publish(dest)`` under the first free ``name`` variant in ``dest_dir``.

    ``publish`` raises ``FileExistsError`` when the name is taken. When
    ``meta`` is given the ``<dest>.meta`` sidecar is written BEFORE the file
    appears, so the watcher can never claim a Gmail file without its intake
    metadata. A name whose file OR sidecar exists is treated as taken: an
    existing sidecar may still belong to a claim in flight, so it is never
    overwritten.
    """
    from .bins import inbox_meta_path

    stem, suffix = os.path.splitext(name)
    counter = 0
    while True:
        dest = dest_dir / (name if counter == 0 else f"{stem}-{counter}{suffix}")
        counter += 1
        if os.path.lexists(dest):
            continue
        if meta is not None and os.path.lexists(inbox_meta_path(dest)):
            continue
        try:
            side = _write_sidecar(dest, meta) if meta is not None else None
        except FileExistsError:
            continue
        try:
            publish(dest)
            return dest
        except FileExistsError:
            # Lost a race for this name: the sidecar now belongs to nobody we
            # know of; leave it (the poller is the sole writer of these names).
            logger.warning("gmail_publish_name_race", dest=str(dest))
            continue
        except BaseException:
            if side is not None:
                side.unlink(missing_ok=True)
            raise


def _link_no_clobber(src: Path, dest_dir: Path, name: str, meta: dict | None = None) -> Path:
    """Hard-link ``src`` into ``dest_dir`` under ``name`` (uniquified), no overwrite."""
    return _publish_no_clobber(lambda dest: os.link(src, dest), dest_dir, name, meta)


def _rename_no_clobber(src: Path, dest_dir: Path, name: str, meta: dict | None = None) -> Path:
    """Promote ``src`` by rename after an existence check (no hard-link support).

    The poller is the sole writer of these inbox names (one ``watcher.lock``
    drain point), so the small check-then-rename window is accepted.
    """

    def _rename(dest: Path) -> None:
        if os.path.lexists(dest):
            raise FileExistsError(str(dest))
        os.rename(src, dest)

    return _publish_no_clobber(_rename, dest_dir, name, meta)


def _place(src: Path, dest_dir: Path, name: str, meta: dict | None = None) -> Path:
    """Atomically place ``src`` in ``dest_dir`` as ``name`` without clobbering.

    Uniquifies on collision. When ``src`` is on another device (``EXDEV``) it
    is copied to a dest-side ``.part`` file, fsynced, then promoted with the
    same no-clobber primitive; the final inbox name is never written directly.
    With ``meta`` the ``.meta`` sidecar is written just before the file
    appears. ``src`` is removed on success.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        dest = _link_no_clobber(src, dest_dir, name, meta)
    except OSError as exc:
        if exc.errno not in _LINK_FALLBACK_ERRNOS:
            raise
        part = dest_dir / f".{uuid.uuid4().hex}.part"
        try:
            with open(src, "rb") as fin, open(part, "xb") as fout:
                shutil.copyfileobj(fin, fout)
                fout.flush()
                os.fsync(fout.fileno())
            try:
                dest = _link_no_clobber(part, dest_dir, name, meta)
            except OSError as link_exc:
                if link_exc.errno not in _LINK_FALLBACK_ERRNOS:
                    raise
                dest = _rename_no_clobber(part, dest_dir, name, meta)
        finally:
            part.unlink(missing_ok=True)
    src.unlink(missing_ok=True)
    return dest


def _quarantine(
    client, uid: str, message_key: str, reason: str, errors: list | None = None
) -> None:
    """Park a poison message: record processed, mark ``\\Seen``, label it failed.

    Best-effort and never raises.
    """
    logger.error("gmail_message_quarantined", uid=uid, message_id=message_key, reason=reason)
    try:
        state = _load_state()
        if message_key not in state["processed_message_ids"]:
            state["processed_message_ids"].append(message_key)
        state["failed_attempts"].pop(message_key, None)
        _save_state(state)
    except Exception:
        logger.exception("gmail_quarantine_state_failed", uid=uid)
    _mark_seen(client, uid)
    try:
        wire = b'("' + _to_mutf7(FAILED_LABEL) + b'")'
        try:
            client.create(b'"' + _to_mutf7(FAILED_LABEL) + b'"')
        except Exception:
            pass
        typ, _ = client.uid("STORE", uid.encode(), "+X-GM-LABELS", wire)
        if typ != "OK":
            raise GmailIntakeError(f"label store returned {typ}")
    except Exception:
        logger.exception("gmail_quarantine_label_failed", uid=uid)
        if errors is not None:
            errors.append(f"quarantine_label_failed:{uid}")


def extract_attachments(msg: email.message.Message) -> list[tuple[str, bytes]]:
    """Filename + decoded-payload pairs for every named attachment."""
    attachments: list[tuple[str, bytes]] = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append((filename, payload))
    return attachments


def _message_id(msg: email.message.Message) -> str | None:
    raw = msg.get("Message-ID") or msg.get("Message-Id")
    if not raw:
        return None
    return str(raw).strip()


def _sender_address(msg: email.message.Message) -> str:
    return email.utils.parseaddr(str(msg.get("From") or ""))[1].lower()


def _received_at(msg: email.message.Message) -> str | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat()


def _extension_of(filename: str) -> str:
    from pathlib import Path

    return Path(filename).suffix.lower()


def deliver_attachment(filename: str, content: Path | bytes, meta: dict) -> tuple[str | None, str | None]:
    """Write one attachment into the inbox + meta sidecar (the /upload route).

    ``content`` may be raw ``bytes`` (API /upload) or a ``Path`` to a
    temporary file already written to disk (Gmail poller streaming).  When a
    ``Path`` is provided the file is moved into the inbox directly, keeping
    peak heap low for large multi-attachment emails. The sidecar is written
    before the file becomes visible.

    Returns ``(delivered_filename, reject_reason)`` — reason is None on
    success, else ``"filename"`` | ``"extension"`` | ``"size"`` | ``"io"``
    (an ``OSError``; never raised for I/O). Collisions are uniquified exactly
    like ``/upload``.
    """
    from pathlib import Path as _Path

    from .bins import inbox_dir, accepted_extensions

    safe = _safe_filename(filename)
    if safe is None:
        return None, "filename"
    if _extension_of(safe) not in accepted_extensions():
        return None, "extension"

    sidecar = {k: v for k, v in meta.items() if not k.startswith("_")}
    try:
        # Support streaming from a temp file path (Gmail poller) or raw bytes.
        if isinstance(content, _Path):
            size = content.stat().st_size
        else:
            size = len(content)
        if size > meta["_max_attachment_bytes"]:
            return None, "size"

        inbox = inbox_dir()
        if isinstance(content, _Path):
            dest = _place(content, inbox, safe, sidecar)
        else:
            staged = _stage_path(_extension_of(safe))
            try:
                staged.write_bytes(content)
                dest = _place(staged, inbox, safe, sidecar)
            finally:
                staged.unlink(missing_ok=True)
    except OSError:
        logger.exception("gmail_attachment_io_error", file=safe)
        return None, "io"
    return dest.name, None


def _screen_attachments(
    attachments: list[tuple[str, bytes]], max_bytes: int
) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]]]:
    """Split attachments into ``accepted`` and ``rejected`` ``(filename, reason)``.

    Reasons: ``filename`` (no usable name), ``extension``, ``size``.
    """
    from .bins import accepted_extensions

    accepted: list[tuple[str, bytes]] = []
    rejected: list[tuple[str, str]] = []
    for filename, content in attachments:
        safe = _safe_filename(filename)
        if safe is None:
            rejected.append((str(filename or ""), "filename"))
        elif _extension_of(safe) not in accepted_extensions():
            rejected.append((filename, "extension"))
        elif len(content) > max_bytes:
            rejected.append((filename, "size"))
        else:
            accepted.append((filename, content))
    return accepted, rejected


def _register_group(message_id: str, expected: int, sender: str, subject: str) -> None:
    try:
        from . import mail_outbox

        mail_outbox.register_group(message_id, expected=expected, sender=sender, subject=subject)
    except Exception:
        logger.exception("gmail_group_register_failed", message_id=message_id)


def _close_group_if_complete(message_id: str) -> str | None:
    """Close the group and queue its full digest when every result is in."""
    try:
        from . import mail_outbox

        info = mail_outbox.group_info(message_id)
        if info is None or info.get("closed_at") is not None:
            return None
        expected = int(info.get("expected") or 0)
        have = len(mail_outbox.group_results(message_id))
        if expected and have >= expected and mail_outbox.close_group(message_id):
            key = _enqueue_digest(message_id, incomplete=False)
            if key:
                _wake_outbox()
            return key
    except Exception:
        logger.exception("gmail_group_recheck_failed", message_id=message_id)
    return None


# ---------------------------------------------------------------------------
# Sender-facing replies: reject (nothing queued) and acknowledgment (queued).
# Every reply goes through the durable outbox, only to senders that passed the
# allowlist/DMARC checks, never to automated mail, and within the per-address
# hourly budget (``mail_guards.reply_allowed``).
# ---------------------------------------------------------------------------

_REJECT_SENTENCES = {
    "extension": "this file type is not accepted",
    "size": "the file is larger than the {max_mb} MB limit",
    "filename": "the attachment has no usable file name",
    "none": "the email had no attachment",
}


def _esc(value) -> str:
    import html as _html

    return _html.escape(str(value if value is not None else ""), quote=True)


def _mail_domain(cfg: dict) -> str:
    address = str(cfg.get("address") or "")
    domain = address.rpartition("@")[2].strip() if "@" in address else ""
    return domain or "mailroom.local"


def _reply_headers(cfg: dict, message_id: str, kind: str) -> dict[str, str]:
    import hashlib

    tag = hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:16]
    return {
        "In-Reply-To": str(message_id),
        "References": str(message_id),
        "Message-ID": f"<mailroom-{kind}-{tag}@{_mail_domain(cfg)}>",
        "Auto-Submitted": "auto-replied",
    }


def _reply_subject(subject: str) -> str:
    subject = " ".join(str(subject or "").split()) or "mailroom intake"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _wake_outbox() -> None:
    try:
        from . import mail_outbox

        worker = mail_outbox.get_worker()
        if worker is not None:
            worker.wake()
    except Exception:
        logger.warning("gmail_outbox_wake_failed", exc_info=True)


def build_reject_reply(
    subject: str, rejected: list[tuple[str, str]], accepted_exts: list[str], max_mb: int
) -> tuple[str, str]:
    """``(text, html)`` telling the sender why nothing was queued and how to fix it."""
    exts = ", ".join(sorted(accepted_exts))
    rows = []
    for name, reason in rejected:
        sentence = _REJECT_SENTENCES.get(reason, "it could not be processed").format(max_mb=max_mb)
        rows.append((name, reason, sentence))
    text_lines = [
        "The mailroom received your email but did not queue any document.",
        "",
    ]
    for name, _reason, sentence in rows:
        text_lines.append(f"  - {name}: {sentence}" if name else f"  - {sentence}")
    text_lines += [
        "",
        f"Accepted file types: {exts}",
        f"Maximum attachment size: {max_mb} MB",
        "",
        "Reply to this email with the corrected attachment to try again.",
        "",
        "Processed by the LLM Mailroom agent",
    ]
    items_html = "".join(
        f"<li>{('<strong>' + _esc(name) + '</strong>: ') if name else ''}{_esc(sentence)}</li>"
        for name, _reason, sentence in rows
    )
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#1f2328;">'
        '<div style="padding:10px 14px;border-radius:6px;background:#9a67001a;border:1px solid #9a6700;">'
        '<strong style="color:#9a6700;">Nothing was queued</strong>'
        '<span style="color:#57606a;"> &mdash; the mailroom received your email but could not accept its attachments.</span>'
        f"</div><ul>{items_html}</ul>"
        f"<div>Accepted file types: <code>{_esc(exts)}</code></div>"
        f"<div>Maximum attachment size: {_esc(max_mb)} MB</div>"
        "<p>Reply to this email with the corrected attachment to try again.</p>"
        '<div style="color:#57606a;font-size:12px;">Processed by the LLM Mailroom agent</div></div>'
    )
    return "\n".join(text_lines), html


def enqueue_reject_reply(
    cfg: dict, *, message_id: str, sender: str, subject: str, rejected: list[tuple[str, str]]
) -> bool:
    """Queue the reject reply (dedup ``reject:<message_id>``). Never raises.

    Callers invoke it only after the automated-mail and allowlist/DMARC
    checks passed; the hourly reply budget is enforced here.
    """
    try:
        from . import mail_outbox
        from .bins import accepted_extensions

        if not sender or not message_id:
            return False
        if not mail_guards.reply_allowed(sender, cfg.get("max_replies_per_hour")):
            logger.warning("gmail_reply_budget_exceeded", to=sender, kind="reject")
            return False
        max_mb = max(1, int(cfg.get("max_attachment_bytes", DEFAULT_MAX_ATTACHMENT_MB * 1024 * 1024)) // (1024 * 1024))
        text, html = build_reject_reply(subject, rejected, list(accepted_extensions()), max_mb)
        queued = mail_outbox.enqueue(
            f"reject:{message_id}",
            to_addr=sender,
            subject=_reply_subject(subject),
            text=text,
            html=html,
            headers=_reply_headers(cfg, message_id, "reject"),
        )
        if queued:
            _wake_outbox()
        return queued
    except Exception:
        logger.exception("gmail_reject_reply_failed", message_id=message_id)
        return False


def build_ack_email(
    subject: str,
    items: list[dict],
    route: str,
    rejected: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """``(text, html)`` acknowledgment: what was queued, the path it takes, its IDs."""
    if route == "triage":
        path = (
            "It will be handled by the fast triage lane (classification, key-entity "
            "extraction and archive); you will get a completion report on this thread."
        )
    else:
        path = (
            f"All {len(items)} documents will run through the full pipeline; you will get "
            "one summary report on this thread when every document has finished."
            if len(items) > 1
            else "It will run through the full pipeline; you will get a completion report on this thread."
        )
    noun = "document" if len(items) == 1 else "documents"
    text_lines = [f"The mailroom received {len(items)} {noun} and queued {'it' if len(items) == 1 else 'them'} for processing.", ""]
    for item in items:
        text_lines.append(f"  - {item.get('filename')}  (id {item.get('upload_id')})")
    text_lines += ["", path]
    if rejected:
        text_lines += ["", "Not accepted:"]
        for name, reason in rejected:
            text_lines.append(f"  - {name or '(unnamed)'}: {reason}")
    text_lines += ["", "Processed by the LLM Mailroom agent"]
    rows_html = "".join(
        f"<tr><td style=\"padding:2px 12px 2px 0;\"><strong>{_esc(i.get('filename'))}</strong></td>"
        f"<td style=\"color:#57606a;\">id <code>{_esc(i.get('upload_id'))}</code></td></tr>"
        for i in items
    )
    rejected_html = (
        "<div style=\"margin-top:8px;color:#9a6700;\">Not accepted: "
        + ", ".join(f"{_esc(n or '(unnamed)')} ({_esc(r)})" for n, r in rejected)
        + "</div>"
        if rejected
        else ""
    )
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#1f2328;">'
        '<div style="padding:10px 14px;border-radius:6px;background:#0969da1a;border:1px solid #0969da;">'
        f'<strong style="color:#0969da;">Received</strong><span style="color:#57606a;"> &mdash; '
        f"{len(items)} {noun} queued for processing</span></div>"
        f'<table style="margin:12px 0;font-size:13px;">{rows_html}</table>'
        f"<div>{_esc(path)}</div>{rejected_html}"
        '<div style="margin-top:14px;color:#57606a;font-size:12px;">Processed by the LLM Mailroom agent</div></div>'
    )
    return "\n".join(text_lines), html


def acks_enabled() -> bool:
    return str(os.environ.get("MAILROOM_GMAIL_ACKS", "1")).strip().lower() not in ("0", "false", "no", "off")


def enqueue_ack_email(
    cfg: dict,
    *,
    message_id: str,
    sender: str,
    subject: str,
    items: list[dict],
    route: str,
    rejected: list[tuple[str, str]] | None = None,
) -> bool:
    """Queue the acknowledgment (dedup ``ack:<message_id>``). Never raises."""
    try:
        from . import mail_outbox

        if not (sender and message_id and items) or not acks_enabled():
            return False
        if not mail_guards.reply_allowed(sender, cfg.get("max_replies_per_hour")):
            logger.warning("gmail_reply_budget_exceeded", to=sender, kind="ack")
            return False
        text, html = build_ack_email(subject, items, route, rejected)
        queued = mail_outbox.enqueue(
            f"ack:{message_id}",
            to_addr=sender,
            subject=_reply_subject(subject),
            text=text,
            html=html,
            headers=_reply_headers(cfg, message_id, "ack"),
        )
        if queued:
            _wake_outbox()
        return queued
    except Exception:
        logger.exception("gmail_ack_failed", message_id=message_id)
        return False


def _fetch_message(client, uid: str) -> bytes | None:
    """Full message bytes via ``BODY.PEEK[]`` (does NOT set ``\\Seen``).

    ``RFC822`` would mark the message seen at fetch time, so a crash before
    delivery would silently drop it; the poller marks seen only once handled.
    """
    typ, data = client.uid("fetch", uid.encode(), "(BODY.PEEK[])")
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _fetch_headers(client, uid: str) -> bytes | None:
    """Header block only via ``BODY.PEEK[HEADER]`` (pre-filtered messages)."""
    typ, data = client.uid("fetch", uid.encode(), "(BODY.PEEK[HEADER])")
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


# ---------------------------------------------------------------------------
# BODYSTRUCTURE pre-filter: decide from the MIME structure alone whether any
# attachment could be accepted, so an oversize-only or wrong-type-only email
# never downloads its body. Unparseable structure → [] → full-fetch fallback.
# ---------------------------------------------------------------------------


@dataclass
class PartInfo:
    name: str | None
    size: int
    maintype: str
    encoding: str = ""

    @property
    def decoded_size(self) -> int:
        """Estimated decoded bytes (BODYSTRUCTURE sizes are transfer-encoded)."""
        if self.encoding.lower() == "base64":
            # 76-char lines + CRLF: 57 decoded bytes per 78 encoded bytes.
            return (self.size * 57) // 78
        return self.size


class _BSParseError(ValueError):
    pass


def _bs_tokens(raw: bytes):
    """Parse an IMAP parenthesized list into nested Python lists.

    Atoms → str (``NIL`` → None), quoted strings → str, literals ``{n}\\r\\n``
    → str. Returns the first complete top-level list.
    """
    i = 0
    n = len(raw)

    def parse_list():
        nonlocal i
        if raw[i : i + 1] != b"(":
            raise _BSParseError("expected list")
        i += 1
        out = []
        while True:
            while i < n and raw[i : i + 1] in (b" ", b"\r", b"\n"):
                i += 1
            if i >= n:
                raise _BSParseError("unterminated list")
            ch = raw[i : i + 1]
            if ch == b")":
                i += 1
                return out
            if ch == b"(":
                out.append(parse_list())
            elif ch == b'"':
                i += 1
                buf = bytearray()
                while i < n and raw[i : i + 1] != b'"':
                    if raw[i : i + 1] == b"\\" and i + 1 < n:
                        i += 1
                    buf += raw[i : i + 1]
                    i += 1
                if i >= n:
                    raise _BSParseError("unterminated string")
                i += 1
                out.append(buf.decode("utf-8", "replace"))
            elif ch == b"{":
                end = raw.index(b"}", i)
                length = int(raw[i + 1 : end])
                i = end + 1
                while i < n and raw[i : i + 1] in (b"\r", b"\n"):
                    i += 1
                out.append(raw[i : i + length].decode("utf-8", "replace"))
                i += length
            else:
                start = i
                while i < n and raw[i : i + 1] not in (b" ", b"(", b")", b"\r", b"\n"):
                    i += 1
                atom = raw[start:i].decode("ascii", "replace")
                out.append(None if atom.upper() == "NIL" else atom)

    start = raw.find(b"(")
    if start < 0:
        raise _BSParseError("no list")
    i = start
    return parse_list()


def _bs_params(value) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    out = {}
    for k, v in zip(value[0::2], value[1::2]):
        if isinstance(k, str) and isinstance(v, str):
            out[k.lower()] = v
    return out


def _bs_filename(params: dict[str, str], disposition) -> str | None:
    disp = {}
    if isinstance(disposition, list) and len(disposition) >= 2:
        disp = _bs_params(disposition[1])
    for source in (disp, params):
        for key in ("filename", "name"):
            if source.get(key):
                return _decode_header_value(source[key])
        for key in ("filename*", "name*"):
            if source.get(key):
                return _decode_rfc2231(source[key])
    return None


def _decode_rfc2231(value: str) -> str:
    """``charset'lang'percent-encoded`` → text (RFC 2231 extended parameter)."""
    import urllib.parse

    charset, sep1, rest = value.partition("'")
    _lang, sep2, encoded = rest.partition("'")
    if not (sep1 and sep2):
        return urllib.parse.unquote(value)
    try:
        return urllib.parse.unquote(encoded, encoding=charset or "utf-8", errors="replace")
    except LookupError:
        return urllib.parse.unquote(encoded)


def _decode_header_value(value: str) -> str:
    if "=?" not in value:
        return value
    try:
        import email.header

        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return value


def _bs_walk(node, out: list[PartInfo]) -> None:
    if not isinstance(node, list) or not node:
        raise _BSParseError("bad body")
    if isinstance(node[0], list):  # multipart: (part)(part)... "SUBTYPE" ...
        for child in node:
            if isinstance(child, list):
                _bs_walk(child, out)
            else:
                break
        return
    if len(node) < 7:
        raise _BSParseError("short body")
    maintype = str(node[0] or "").lower()
    subtype = str(node[1] or "").lower()
    params = _bs_params(node[2])
    encoding = str(node[5] or "")
    try:
        size = int(node[6] or 0)
    except (TypeError, ValueError):
        raise _BSParseError("bad size")
    if maintype == "message" and subtype == "rfc822" and len(node) > 8:
        _bs_walk(node[8], out)  # attachments inside a forwarded message
        return
    disposition = None
    for extra in node[7:]:
        if (
            isinstance(extra, list)
            and len(extra) >= 2
            and isinstance(extra[0], str)
            and extra[0].lower() in ("attachment", "inline")
        ):
            disposition = extra
            break
    out.append(PartInfo(name=_bs_filename(params, disposition), size=size, maintype=maintype, encoding=encoding))


def parse_bodystructure(raw: bytes) -> list[PartInfo]:
    """Leaf parts from a FETCH ``BODYSTRUCTURE`` response ([] when unparseable)."""
    try:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        idx = raw.upper().find(b"BODYSTRUCTURE")
        if idx >= 0:
            raw = raw[idx + len(b"BODYSTRUCTURE") :]
        parts: list[PartInfo] = []
        _bs_walk(_bs_tokens(raw), parts)
        return parts
    except Exception:
        return []


def _fetch_structure(client, uid: str) -> list[PartInfo]:
    """Parsed BODYSTRUCTURE for ``uid`` ([] on any failure → full fetch)."""
    try:
        typ, data = client.uid("fetch", uid.encode(), "(BODYSTRUCTURE)")
        if typ != "OK" or not data:
            return []
        buf = bytearray()
        for item in data:
            if isinstance(item, tuple):
                buf += bytes(item[0]) + b"\r\n" + bytes(item[1] or b"")
            elif isinstance(item, (bytes, bytearray)):
                buf += bytes(item)
        return parse_bodystructure(bytes(buf))
    except _TRANSPORT_ERRORS:
        raise
    except Exception:
        logger.warning("gmail_bodystructure_failed", uid=uid, exc_info=True)
        return []


def _structure_rejects(parts: list[PartInfo], max_bytes: int) -> list[tuple[str, str]] | None:
    """Rejections when NO named part could be accepted, else None (fetch body)."""
    from .bins import accepted_extensions

    rejected: list[tuple[str, str]] = []
    for part in parts:
        if not part.name:
            continue
        safe = _safe_filename(part.name)
        if safe is None:
            rejected.append((part.name, "filename"))
        elif _extension_of(safe) not in accepted_extensions():
            rejected.append((part.name, "extension"))
        elif part.decoded_size > max_bytes:
            rejected.append((part.name, "size"))
        else:
            return None  # at least one candidate: download the body
    return rejected


_TRANSPORT_ERRORS = (
    imaplib.IMAP4.abort,  # NOT IMAP4.error: a BAD/NO for one message is per-message
    socket.timeout,
    socket.gaierror,
    socket.herror,
    ConnectionError,
    ssl.SSLError,
    EOFError,
)


def _uidvalidity(client) -> str:
    """UIDVALIDITY of the selected folder ("0" when the server does not say)."""
    try:
        _, data = client.response("UIDVALIDITY")
        raw = data[0] if data else None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("ascii", errors="ignore")
        raw = str(raw or "").strip()
        return raw if raw.isdigit() else "0"
    except Exception:
        return "0"


def _mark_seen(client, uid: str) -> bool:
    try:
        typ, _ = client.uid("store", uid.encode(), "+FLAGS", "(\\Seen)")
        return typ == "OK"
    except Exception:
        logger.exception("gmail_intake_mark_seen_failed", uid=uid)
        return False


def poll_once(
    *,
    config: dict | None = None,
    imap_factory=None,
) -> dict:
    """One IMAP sweep: unseen messages → inbox attachments + meta sidecars.

    Returns a report dict; never raises (errors land in the report + status).
    """
    from pathlib import Path

    cfg = config or load_config()
    report: dict = {
        "connected": False,
        "messages_seen": 0,
        "attachments_queued": 0,
        "skipped_extension": 0,
        "skipped_size": 0,
        "skipped_sender": 0,
        "skipped_auth": 0,
        "skipped_automated": 0,
        "skipped_no_attachments": 0,
        "reject_replies": 0,
        "ack_replies": 0,
        "prefiltered": 0,
        "marked_seen": 0,
        "already_processed": 0,
        "quarantined": 0,
        "errors": [],
    }
    if not cfg.get("address") or not cfg.get("password"):
        report["errors"].append("missing_credentials")
        _record_status(last_error="missing_credentials", last_poll_at=_now_iso())
        return report

    _sweep_orphans()
    state = _load_state()
    processed_ids: list[str] = state.get("processed_message_ids", [])
    failed_attempts: dict[str, int] = state.get("failed_attempts", {})

    factory = (
        imap_factory
        or _INJECTED_IMAP_FACTORY
        or (lambda: imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=IMAP_TIMEOUT_SECONDS))
    )
    client = None
    try:
        client = factory()
        client.login(cfg["address"], cfg["password"])
        typ, _ = client.select(cfg["folder"], readonly=False)
        if typ != "OK":
            raise GmailIntakeError(f"cannot select folder {cfg['folder']!r}")
        report["connected"] = True
        typ, uids = client.uid("search", None, "UNSEEN")
        if typ != "OK":
            raise GmailIntakeError("uid search failed")
        uid_list = (uids[0] or b"").split() if uids else []
        report["messages_seen"] = len(uid_list)

        uidvalidity = _uidvalidity(client)
        max_attempts = _max_attempts()

        def _record_failure(uid, message_key, known_mid, desc):
            attempts = failed_attempts.get(message_key, 0) + 1
            failed_attempts[message_key] = attempts
            if attempts >= max_attempts:
                _quarantine(client, uid, message_key, desc, errors=report["errors"])
                for key in (message_key, known_mid):
                    if key and key not in processed_ids:
                        processed_ids.append(key)
                failed_attempts.pop(message_key, None)
                report["quarantined"] += 1
                report["errors"].append(f"quarantined:{uid}:{desc}")

        for uid in uid_list:
            uid = uid.decode("ascii", errors="ignore")
            fallback_key = f"uid:{cfg['folder']}:{uidvalidity}:{uid}"
            message_key = fallback_key
            known_mid = None
            staged: list[Path] = []
            try:
                structure_rejects = None
                parts = _fetch_structure(client, uid)
                if parts:
                    structure_rejects = _structure_rejects(parts, cfg["max_attachment_bytes"])
                if structure_rejects is not None:
                    # Nothing acceptable by structure: headers only, no body.
                    raw = _fetch_headers(client, uid)
                else:
                    raw = _fetch_message(client, uid)
                if raw is None:
                    report["errors"].append(f"fetch_failed:{uid}")
                    _record_failure(uid, message_key, None, "fetch_failed")
                    continue
                msg = email.message_from_bytes(raw)
                known_mid = _message_id(msg)
                message_id = known_mid or fallback_key
                message_key = message_id
                if message_id != fallback_key:
                    carried = failed_attempts.pop(fallback_key, 0)
                    if carried:
                        failed_attempts[message_id] = failed_attempts.get(message_id, 0) + carried
                if message_id in processed_ids:
                    failed_attempts.pop(message_id, None)
                    failed_attempts.pop(fallback_key, None)
                    report["already_processed"] += 1
                    _mark_seen(client, uid)
                    continue

                sender = _sender_address(msg)
                # Loop guard first: automated mail (auto-replies, bounces,
                # lists, our own messages) is never processed or answered.
                auto = mail_guards.automated_reason(
                    msg, cfg.get("address", ""), allow_self=mail_guards.allow_self()
                )
                if auto is not None:
                    logger.info("gmail_message_automated_skipped", sender=sender, uid=uid, reason=auto)
                    report["skipped_automated"] += 1
                    processed_ids.append(message_id)
                    _mark_seen(client, uid)
                    continue
                permitted, why = mail_guards.sender_permitted(
                    cfg.get("allowed_senders") or set(),
                    sender,
                    mail_guards.message_auth_verdict(msg),
                    bool(cfg.get("require_dmarc", mail_guards.require_dmarc())),
                )
                if not permitted:
                    logger.info("gmail_message_sender_rejected", sender=sender, uid=uid, reason=why)
                    report["skipped_auth" if why == "dmarc" else "skipped_sender"] += 1
                    processed_ids.append(message_id)
                    _mark_seen(client, uid)
                    continue

                subject = str(msg.get("Subject") or "")
                matter_id = parse_matter_id(subject) or cfg["default_matter_id"]
                # Single vs bundle routing (HUB-037): count the attachments
                # that WOULD be delivered (filename + extension + size guards)
                # and route the message — ONE accepted attachment = single-
                # document upload (free-triage lane); TWO OR MORE = multi-
                # document upload (full paid pipeline, triage dropped).
                if structure_rejects is not None:
                    accepted, rejected = [], structure_rejects
                    report["prefiltered"] += 1
                else:
                    accepted, rejected = _screen_attachments(
                        extract_attachments(msg), cfg["max_attachment_bytes"]
                    )
                for _name, reason in rejected:
                    if reason in ("extension", "filename"):
                        report["skipped_extension"] += 1
                    elif reason == "size":
                        report["skipped_size"] += 1

                # Phase 1 — stage EVERY accepted attachment before publishing
                # any. Staged .part files are never watcher-visible, so an I/O
                # error here publishes nothing and the message is retried
                # (counted toward the poison cap) on the next sweep.
                staged_items: list[tuple[str, Path]] = []
                for filename, content in accepted:
                    try:
                        tmp = _stage_path(_extension_of(filename))
                        staged.append(tmp)
                        tmp.write_bytes(content)
                    except OSError as exc:
                        raise GmailIntakeError(f"io: staging {filename!r}: {exc}") from exc
                    staged_items.append((filename, tmp))
                del accepted  # free decoded bytes before publishing

                # Phase 2 — promote (sidecar, then file). A file already in
                # the inbox is never unlinked (the watcher may have claimed
                # it); a part-way failure records the message processed with
                # the unpromoted files named, so nothing is ever queued twice.
                route = "triage" if len(staged_items) == 1 else "pipeline"
                if len(staged_items) > 1:
                    # Register the bundle BEFORE any file is visible so a fast
                    # terminal result can never miss its digest group.
                    _register_group(message_id, len(staged_items), sender, subject)
                queued_items: list[dict] = []
                unpromoted: list[str] = []
                for filename, content_path in staged_items:
                    meta = {
                        "matter_id": matter_id,
                        "source": "gmail",
                        "message_id": message_id,
                        "sender": sender,
                        "subject": subject[:200],
                        "received_at": _received_at(msg),
                        "route": route,
                        "group_size": len(staged_items),
                        "upload_id": uuid.uuid4().hex[:12],
                        "size": content_path.stat().st_size,
                        "original_filename": filename,
                        "_max_attachment_bytes": cfg["max_attachment_bytes"],
                    }
                    try:
                        delivered, reject_reason = deliver_attachment(filename, content_path, meta)
                    except Exception as exc:
                        if not queued_items:
                            raise  # nothing published: retry the whole message
                        logger.exception("gmail_attachment_promote_failed", file=filename, uid=uid)
                        delivered, reject_reason = None, f"io:{type(exc).__name__}"
                    if delivered is None:
                        if not queued_items and str(reject_reason).startswith("io"):
                            # Nothing published yet: safe to retry the whole message.
                            raise GmailIntakeError(f"io: promoting {filename!r}")
                        unpromoted.append(filename)
                        continue
                    queued_items.append(
                        {"filename": delivered, "original_filename": filename, "upload_id": meta["upload_id"]}
                    )
                    logger.info(
                        "gmail_attachment_queued",
                        file=delivered,
                        matter_id=matter_id,
                        sender=sender,
                        message_id=message_id,
                        route=route,
                    )
                queued = len(queued_items)
                if unpromoted:
                    report["errors"].append(
                        f"promote_failed:{uid}:{','.join(unpromoted)}"
                    )
                report["attachments_queued"] += queued
                if queued == 0:
                    report["skipped_no_attachments"] += 1
                    logger.info(
                        "gmail_message_no_processable_attachments",
                        message_id=message_id,
                        sender=sender,
                        subject=subject[:120],
                        detail="no accepted attachment — reject reply queued, message marked seen",
                    )
                    if not unpromoted:
                        if enqueue_reject_reply(
                            cfg,
                            message_id=message_id,
                            sender=sender,
                            subject=subject,
                            rejected=rejected or [("", "none")],
                        ):
                            report["reject_replies"] += 1
                else:
                    if enqueue_ack_email(
                        cfg,
                        message_id=message_id,
                        sender=sender,
                        subject=subject,
                        items=queued_items,
                        route=route,
                        rejected=rejected,
                    ):
                        report["ack_replies"] += 1
                    if len(staged_items) > 1 and queued != len(staged_items):
                        _register_group(message_id, queued, sender, subject)
                        # Results may already have arrived for the queued
                        # files while ``expected`` was still the staged count.
                        _close_group_if_complete(message_id)
                processed_ids.append(message_id)
                failed_attempts.pop(message_id, None)
                if _mark_seen(client, uid):
                    report["marked_seen"] += 1
            except _TRANSPORT_ERRORS:
                # Connection-level failure: abort the sweep (outer handler
                # records it) WITHOUT counting against any message.
                raise
            except Exception as exc:  # one bad message must not stop the sweep
                logger.exception("gmail_message_failed", uid=uid)
                report["errors"].append(f"message_failed:{uid}:{type(exc).__name__}")
                _record_failure(
                    uid, message_key, known_mid, f"{type(exc).__name__}: {exc}"
                )
            finally:
                for path in staged:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                # Persist after EVERY message so a crash mid-sweep cannot
                # double-queue the messages already handled.
                _save_state(
                    {"processed_message_ids": processed_ids, "failed_attempts": failed_attempts}
                )
    except Exception as exc:
        logger.exception("gmail_poll_failed")
        report["errors"].append(f"poll_failed:{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

    _save_state({"processed_message_ids": processed_ids, "failed_attempts": failed_attempts})
    _record_status(
        last_poll_at=_now_iso(),
        last_error=report["errors"][0] if report["errors"] else None,
        messages_seen=_STATUS["messages_seen"] + report["messages_seen"],
        attachments_queued=_STATUS["attachments_queued"] + report["attachments_queued"],
    )
    if report["errors"]:
        logger.warning("gmail_poll_report", **report)
    return report


IDLE_MAX_SECONDS = 300.0


def idle_enabled() -> bool:
    """Opt-in IMAP IDLE push (``MAILROOM_GMAIL_IDLE=1``; default off until
    verified live with ``gmail_smoke_test.py --real``)."""
    return str(os.environ.get("MAILROOM_GMAIL_IDLE", "0")).strip().lower() in ("1", "true", "yes", "on")


def idle_wait(client, timeout_s: float) -> bool:
    """Block in IMAP IDLE (RFC 2177) for up to ``timeout_s`` (≤ 5 min).

    Returns True when the server pushed new mail (``EXISTS``/``RECENT``),
    False on timeout. Raw IDLE over imaplib's ``send``/``readline`` (stdlib
    has no IDLE before 3.14); any protocol surprise raises so the caller
    falls back to plain polling.
    """
    timeout_s = max(1.0, min(float(timeout_s), IDLE_MAX_SECONDS))
    tag = client._new_tag()
    client.send(tag + b" IDLE\r\n")
    line = client.readline()
    if not line.startswith(b"+"):
        raise GmailIntakeError(f"IDLE refused: {line[:80]!r}")
    # Wait with select() rather than a socket timeout: a timeout raised inside
    # imaplib's buffered makefile reader leaves it unusable, so DONE could not
    # be read back. A line already buffered behind a keepalive at worst waits
    # out this IDLE window; the next sweep still picks the mail up.
    sock = client.socket()
    pushed = False
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pending = getattr(sock, "pending", None)
        if not (callable(pending) and pending() > 0):
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                break
        line = client.readline()
        if not line:
            raise GmailIntakeError("IDLE connection closed")
        upper = line.upper()
        if b"EXISTS" in upper or b"RECENT" in upper:
            pushed = True
            break
    client.send(b"DONE\r\n")
    while True:
        line = client.readline()
        if not line:
            raise GmailIntakeError("IDLE connection closed after DONE")
        if line.startswith(tag):
            if b" OK" not in line.upper():
                raise GmailIntakeError(f"IDLE ended badly: {line[:80]!r}")
            return pushed


class GmailIntakePoller(threading.Thread):
    """Background poll loop; daemon thread, one sweep per ``poll_seconds``.

    With ``MAILROOM_GMAIL_IDLE=1`` the wait between sweeps is an IMAP IDLE on
    a dedicated connection, so new mail is swept as soon as Gmail pushes it;
    any IDLE failure drops that connection and falls back to the plain wait.
    """

    def __init__(self, poll_seconds: float | None = None, use_idle: bool | None = None):
        super().__init__(name="gmail-intake", daemon=True)
        cfg = load_config()
        self.poll_seconds = poll_seconds or cfg["poll_seconds"]
        self.use_idle = idle_enabled() if use_idle is None else use_idle
        self._stop_event = threading.Event()
        self._idle_client = None

    def _open_idle_client(self):
        cfg = load_config()
        factory = _INJECTED_IMAP_FACTORY or (
            lambda: imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=IMAP_TIMEOUT_SECONDS)
        )
        client = factory()
        client.login(cfg["address"], cfg["password"])
        typ, _ = client.select(cfg["folder"], readonly=True)
        if typ != "OK":
            raise GmailIntakeError(f"cannot select folder {cfg['folder']!r}")
        return client

    def _close_idle_client(self) -> None:
        client, self._idle_client = self._idle_client, None
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

    def _wait_for_mail(self) -> None:
        if self.use_idle and not self._stop_event.is_set():
            try:
                if self._idle_client is None:
                    self._idle_client = self._open_idle_client()
                pushed = idle_wait(self._idle_client, self.poll_seconds)
                logger.debug("gmail_idle_wake", pushed=pushed)
                return
            except Exception as exc:
                logger.warning("gmail_idle_failed", error=f"{type(exc).__name__}: {exc}")
                self._close_idle_client()
        self._stop_event.wait(self.poll_seconds)

    def run(self) -> None:
        _record_status(running=True, enabled=True)
        logger.info("gmail_poller_started", poll_seconds=self.poll_seconds, idle=self.use_idle)
        try:
            while not self._stop_event.is_set():
                poll_once()
                if self._stop_event.is_set():
                    break
                self._wait_for_mail()
        finally:
            self._close_idle_client()
            _record_status(running=False)
            logger.info("gmail_poller_stopped")

    def stop(self) -> None:
        self._stop_event.set()


def start_embedded_poller() -> GmailIntakePoller | None:
    """Start the poller inside the watcher process (enabled-only, never raises).

    Called by ``Watcher.start()`` — the poller and the inbox drain share one
    process so ``watcher.lock`` stays the single intake authority.
    """
    if not gmail_intake_enabled():
        return None
    cfg = load_config()
    if not cfg.get("address") or not cfg.get("password"):
        logger.warning(
            "gmail_intake_enabled_but_unconfigured",
            hint="set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env",
        )
        return None
    _record_status(enabled=True)
    try:
        poller = GmailIntakePoller(poll_seconds=cfg["poll_seconds"])
        poller.start()
        return poller
    except Exception:
        logger.exception("gmail_poller_start_failed")
        return None


def stop_embedded_poller(poller: GmailIntakePoller | None) -> None:
    if poller is None:
        return
    try:
        poller.stop()
    except Exception:
        logger.exception("gmail_poller_stop_failed")


def main() -> int:
    """Standalone entrypoint: ``PYTHONPATH=src python -m pipeline.gmail_intake``."""
    from .logging import setup_logging

    setup_logging()
    log = structlog.get_logger("pipeline.gmail_intake")
    if not gmail_intake_enabled():
        log.error(
            "gmail_intake_disabled",
            hint="set MAILROOM_GMAIL_ENABLED=1 plus GMAIL_ADDRESS / GMAIL_APP_PASSWORD",
        )
        return 1
    poller = start_embedded_poller()
    if poller is None:
        return 1
    try:
        while poller.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_embedded_poller(poller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Completion echo (HUB-037): when a Gmail-intake document reaches a terminal
# stage, reply on the source thread with the outcome — classification,
# extraction report, archive entry (path + sha256) and the verified audit
# chain. The ✅ reaction proves pickup; the echo proves the pipeline happened.
# ---------------------------------------------------------------------------



def echoes_enabled() -> bool:
    """Whether terminal-stage completion echoes are on (default: with the channel)."""
    if not gmail_intake_enabled():
        return False
    return str(os.environ.get("MAILROOM_GMAIL_ECHOES", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def triage_enabled() -> bool:
    """Whether the single-document Gmail triage lane is on (default: with the channel).

    The triage lane runs at watcher claim time for emails carrying exactly
    ONE accepted attachment (free OpenRouter model, advisory — see
    ``agents/gmail_triage.py``). Set ``MAILROOM_GMAIL_TRIAGE=0`` to disable
    (single-document emails then take the full paid pipeline); failures
    always fail soft.
    """
    if not gmail_intake_enabled():
        return False
    return str(os.environ.get("MAILROOM_GMAIL_TRIAGE", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def build_echo_body(manifest: dict, audit_rows: list[dict] | None = None, chain_valid: bool | None = None) -> str:
    """Render the completion report for one terminal manifest (plain text)."""
    stage = str(manifest.get("stage") or "unknown").upper()
    intake = manifest.get("intake") or {}
    lines: list[str] = []
    lines.append("MAILROOM COMPLETION REPORT")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"STATUS: {stage}")
    lines.append(f"document:  {manifest.get('original_filename', '')}")
    lines.append(f"doc_id:    {manifest.get('doc_id', '')}")
    lines.append(f"matter:    {manifest.get('matter_id', '')}")
    lines.append(f"received:  {intake.get('received_at', 'n/a')} via Gmail from {intake.get('sender', 'n/a')}")
    lines.append("")

    lines.append("-- CLASSIFICATION " + "-" * 44)
    lines.append(f"doc_type:  {manifest.get('doc_type') or 'n/a'}")
    if manifest.get("doc_subclass"):
        lines.append(f"subclass:  {manifest.get('doc_subclass')}")
    lines.append(f"confidence: {manifest.get('classification_confidence') if manifest.get('classification_confidence') is not None else 'n/a'}")
    lines.append("")

    # Advisory pre-pipeline intake read (HUB-037): the free-model triage log,
    # carried on the intake meta for Gmail-channel documents.
    triage = intake.get("triage")
    if isinstance(triage, dict) and triage:
        lines.append("-- INTAKE TRIAGE (pre-pipeline) " + "-" * 29)
        lines.append(f"doc_type:  {triage.get('primary_doc_class') or 'n/a'}")
        if triage.get("doc_subclass"):
            lines.append(f"subclass:  {triage.get('doc_subclass')}")
        conf = triage.get("confidence")
        lines.append(f"confidence: {conf if conf is not None else 'n/a'}")
        gist = str(triage.get("gist") or "").strip()
        if gist:
            lines.append(f"gist:      {gist}")
        keywords = triage.get("keywords") or []
        if keywords:
            lines.append(f"keywords:  {', '.join(str(k) for k in keywords)}")
        # Key/concise entity extraction (HUB-048): the free-model triage read
        # carries the per-class key entities (sender/recipient/date/amounts/
        # action items for correspondence; parties/effective_date/… for
        # contracts; claim_number/insurer/… for insurance claims) so short
        # documents like Enron emails give a concise entity answer.
        extraction = triage.get("extraction")
        if isinstance(extraction, dict) and extraction:
            lines.append("EXTRACTED KEY ENTITIES (triage):")
            for k, v in extraction.items():
                if isinstance(v, list):
                    lines.append(f"  {k}: {', '.join(str(x) for x in v)}")
                else:
                    lines.append(f"  {k}: {v}")
        # Debug evidence (human directive 2026-09-04: full input/output logs
        # for debugging): when the free model's answer could not be parsed,
        # the echo says WHY and where the complete payloads live.
        dbg = triage.get("debug")
        if isinstance(dbg, dict) and dbg.get("parse_ok") is False:
            lines.append(
                f"triage debug: response not parseable — {dbg.get('parse_error')} "
                f"(model served: {dbg.get('model')}; full I/O: {dbg.get('debug_dir')})"
            )
        lines.append("")

    # Honest handoff (HUB-037): the free triage capability pre-check rejected
    # this document (too long / vision-only / unreadable) and the full paid
    # pipeline handled it instead.
    handoff = intake.get("triage_handoff")
    if handoff:
        lines.append(f"triage handoff: {handoff} — handled by the full pipeline")
        lines.append("")

    # Processing timeline + plain-language narrative (human directive
    # 2026-09-04: expand the depth of information the closing message conveys).
    timeline = _processing_timeline(audit_rows)
    if timeline:
        lines.append("-- PROCESSING TIMELINE " + "-" * 39)
        for event, ts, delta in timeline:
            lines.append(f"  {ts}  {event}{delta}")
        lines.append("")
    narrative = _what_happened(manifest, audit_rows)
    stage_upper = stage.upper()
    if narrative:
        lines.append("-- WHAT HAPPENED " + "-" * 42)
        lines.append(narrative)
        reason = str(
            manifest.get("escalation_reason") or manifest.get("error_message") or ""
        )
        if stage_upper in ("REVIEW", "FAILED"):
            friendly = friendly_reason(reason)
            if friendly:
                lines.append("")
                lines.append(f"why: {friendly[0]}")
                lines.append("")
                lines.append(f"next steps: {friendly[1]}")
        lines.append("")

    extracted = manifest.get("extracted_data")
    lines.append("-- EXTRACTION " + "-" * 46)
    if isinstance(extracted, dict) and extracted:
        report = extracted.get("_report")
        if report:
            lines.append(str(report))
        payload = {k: v for k, v in extracted.items() if k not in ("_report",) and v not in (None, [], "")}
        if payload:
            lines.append(json.dumps(payload, indent=2, default=str))
        conf = extracted.get("confidence")
        if conf is not None:
            lines.append(f"extraction confidence: {conf}")
    else:
        lines.append("no extraction on this terminal record")
    lines.append("")

    lines.append("-- ARCHIVE ENTRY " + "-" * 44)
    if stage == "ARCHIVED":
        for row in audit_rows or []:
            if row.get("event") == "archived":
                detail = row.get("detail")
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except Exception:
                        detail = {"detail": detail}
                if isinstance(detail, dict):
                    for key in ("archive_path", "file_sha256", "size_bytes", "prev_hash", "entry_hash"):
                        if detail.get(key) is not None:
                            lines.append(f"{key}: {detail[key]}")
                break
        else:
            lines.append("archive detail unavailable in audit chain")
    else:
        why = manifest.get("escalation_reason") or manifest.get("error_message") or ""
        lines.append(f"not archived — stage {stage}" + (f": {why}" if why else ""))
    lines.append("")

    # Related work (HUB-040): the relations clerk's advisory block — what the
    # archive already knows this document/matter relates to. Bounded + best-
    # effort: an empty ledger or a storage hiccup simply renders nothing.
    try:
        from pipeline.relations import context_block

        related = context_block(
            matter_id=manifest.get("matter_id"), doc_id=manifest.get("doc_id")
        )
        if related:
            lines.extend(related.splitlines())
            lines.append("")
    except Exception:
        logger.warning("gmail_echo_related_section_failed")

    lines.append("-- AUDIT TRAIL " + "-" * 46)
    if audit_rows:
        for row in audit_rows:
            ts = str(row.get("timestamp", ""))
            lines.append(f"  {ts}  {row.get('event', '')}  (actor: {row.get('actor', '')})")
        if chain_valid is None:
            lines.append("chain verification: unavailable")
        else:
            lines.append(f"chain verification: {'OK — hash chain intact' if chain_valid else 'BROKEN — investigate immediately'}")
    else:
        lines.append("audit chain unavailable")
    lines.append("")

    if manifest.get("trace_id"):
        lines.append(f"trace_id: {manifest['trace_id']}")
    lines.append(f"echo generated: {_now_iso()}")
    lines.append("")
    lines.append("Processed by the LLM Mailroom agent")
    lines.append("mailroom-dev: https://github.com/Exios66/mailroom-dev")
    return "\n".join(lines)


_EMOJI_LABEL_MUTF7 = "&JwU-"  # ✅ in RFC 3501 modified-UTF-7


def friendly_reason(reason: str) -> tuple[str, str] | None:
    """Translate a raw escalation reason into (plain-language, next-steps).

    Returns None when the reason needs no translation (rendered as-is). The
    human directive 2026-09-04: the closing message must CONVEY what happened
    and what to do — an opaque "(RuntimeError)" tells the sender nothing."""
    if not reason:
        return None
    text = str(reason)
    if "MAILROOM_LLM_FREE_ONLY" in text or "not free" in text:
        return (
            "The pipeline's paid agent was blocked by the free-only pilot "
            "guardrail (MAILROOM_LLM_FREE_ONLY=1): the OpenRouter key is "
            "scoped to the free triage team, so paid models refuse to load. "
            "This document exceeds the free triage budget, so completing it "
            "requires paid models — the pipeline parked it safely (nothing "
            "was spent).",
            "Options: (1) approve/resolve this document from the review "
            "queue (The-Mailroom REVIEW); (2) to let paid models run in "
            "full production, unset MAILROOM_LLM_FREE_ONLY in .env and "
            "restart the watcher — the document then re-processes normally; "
            "(3) re-send a shorter document (free triage handles text up to "
            "~12,000 characters).",
        )
    if "sorter reviewer failed" in text or "reviewer" in text.lower():
        return (
            "The independent sorter-reviewer lane (the pipeline's "
            "second-opinion check on classification) failed and the "
            "pipeline failed safe: the document is parked for human review "
            "with the sorter's original answer intact. Nothing was "
            "archived yet; the underlying error is shown below.",
            "Next steps: resolve from the review queue (The-Mailroom "
            "REVIEW, or POST /v1/review/<doc_id>/resolve) — approval "
            "resumes the pipeline from extraction; the underlying error "
            "text below names the exact cause.",
        )
    if "exceeds_free_budget" in text:
        return (
            "This document is longer than the free triage team's input "
            "budget, so it was honestly handed off to the full paid "
            "pipeline (the free lane never starts a doomed run).",
            "Next steps: no action needed — the full pipeline is "
            "processing this document; this report is its outcome.",
        )
    return None


def _processing_timeline(audit_rows: list[dict] | None) -> list[tuple[str, str, str]]:
    """(event, timestamp, delta-seconds) rows for the echo's timeline — how
    long each pipeline step took, from the audit chain itself."""
    import datetime as _dt

    rows = []
    prev = None
    for row in audit_rows or []:
        ts_raw = str(row.get("timestamp") or "")
        try:
            ts = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        delta = ""
        if prev is not None:
            delta = f" (+{(ts - prev).total_seconds():.0f}s)"
        prev = ts
        rows.append((str(row.get("event") or ""), ts_raw, delta))
    return rows


def _what_happened(manifest: dict, audit_rows: list[dict] | None) -> str:
    """Plain-language narrative of the route this document took — the human
    directive: expand the depth of information the closing message conveys."""
    intake = manifest.get("intake") or {}
    stage = str(manifest.get("stage") or "").upper()
    parts = []
    if intake.get("source") == "gmail":
        parts.append("Received by email from the sender")
    route = intake.get("route") or ""
    if intake.get("triage_handoff"):
        parts.append(
            f"handed off from the free triage lane to the full pipeline ({intake.get('triage_handoff')})"
        )
    elif route == "pipeline":
        parts.append("processed by the full pipeline (multi-document email)")
    elif route == "triage":
        parts.append("handled entirely by the free triage lane")
    events = [str(r.get("event") or "") for r in audit_rows or []]
    if stage == "ARCHIVED":
        parts.append("archived in the auditable hash archive")
    elif stage == "REVIEW":
        parts.append("parked in the human-review queue")
    elif stage == "FAILED":
        parts.append("parked in the failed bin")
    if "classified" in events and stage == "REVIEW":
        parts.append("classification completed before the escalation")
    return " → ".join(parts) if parts else ""


def build_echo_html(
    manifest: dict, audit_rows: list[dict] | None = None, chain_valid: bool | None = None
) -> str:
    """Clean HTML rendering of the completion report (multipart alternative).

    Email-safe inline styles only; the sender-facing acknowledgement leads
    with a status banner, and every report links back to the mailroom-dev
    repository (human directive 2026-09-04)."""
    stage = str(manifest.get("stage") or "unknown").upper()
    intake = manifest.get("intake") or {}
    status_color, status_emoji = {
        "ARCHIVED": ("#1a7f37", "&#9989;"),
        "REVIEW": ("#9a6700", "&#9203;"),
        "FAILED": ("#cf222e", "&#10060;"),
    }.get(stage, ("#57606a", "&#128196;"))
    extracted = manifest.get("extracted_data") if isinstance(manifest.get("extracted_data"), dict) else {}
    triage = intake.get("triage") if isinstance(intake.get("triage"), dict) else None
    handoff = intake.get("triage_handoff")

    def _esc(v) -> str:
        import html as _html

        return _html.escape(str(v if v is not None else "n/a"))

    rows = [
        ("Document", manifest.get("original_filename")),
        ("Matter", manifest.get("matter_id")),
        ("Document ID", manifest.get("doc_id")),
        (
            "Received",
            f"{intake.get('received_at', 'n/a')} via Gmail from {intake.get('sender', 'n/a')}",
        ),
    ]
    meta_html = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#57606a;">{label}</td>'
        f'<td style="padding:2px 0;"><strong>{_esc(value)}</strong></td></tr>'
        for label, value in rows
    )

    classification = (
        f'<tr><td style="padding:2px 12px 2px 0;color:#57606a;">Type</td>'
        f'<td style="padding:2px 0;">{_esc(manifest.get("doc_type"))}</td></tr>'
    )
    if manifest.get("doc_subclass"):
        classification += (
            f'<tr><td style="padding:2px 12px 2px 0;color:#57606a;">Subclass</td>'
            f'<td style="padding:2px 0;">{_esc(manifest.get("doc_subclass"))}</td></tr>'
        )
    if manifest.get("classification_confidence") is not None:
        classification += (
            f'<tr><td style="padding:2px 12px 2px 0;color:#57606a;">Confidence</td>'
            f'<td style="padding:2px 0;">{_esc(manifest.get("classification_confidence"))}</td></tr>'
        )

    triage_html = ""
    if triage:
        kw = ", ".join(str(k) for k in (triage.get("keywords") or []))
        triage_html = (
            '<div style="margin:10px 0;padding:8px 12px;background:#f6f8fa;'
            'border-left:3px solid #0969da;border-radius:4px;">'
            '<div style="font-weight:600;color:#0969da;">Intake triage (pre-pipeline)</div>'
            f'<div>{_esc(triage.get("primary_doc_class"))}'
            + (f' &middot; {_esc(triage.get("doc_subclass"))}' if triage.get("doc_subclass") else "")
            + f' &middot; confidence {_esc(triage.get("confidence"))}</div>'
            + (f'<div style="color:#57606a;">{_esc(triage.get("gist"))}</div>' if triage.get("gist") else "")
            + (f'<div style="color:#57606a;">keywords: {_esc(kw)}</div>' if kw else "")
            + "</div>"
        )
    handoff_html = (
        f'<div style="margin:10px 0;color:#9a6700;">&#9888; Triage handoff: {_esc(handoff)} '
        "&mdash; handled by the full pipeline.</div>"
        if handoff
        else ""
    )

    extraction_html = ""
    if extracted:
        report = str(extracted.get("_report") or "")
        payload = {k: v for k, v in extracted.items() if k != "_report" and v not in (None, [], "")}
        body_bits = []
        if report:
            body_bits.append(
                f'<pre style="white-space:pre-wrap;margin:6px 0;">{_esc(report[:1200])}</pre>'
            )
        if payload:
            body_bits.append(
                "<pre style=\"white-space:pre-wrap;margin:6px 0;color:#57606a;\">"
                + _esc(json.dumps(payload, indent=2, default=str)[:1200])
                + "</pre>"
            )
        extraction_html = (
            '<div style="margin:10px 0;padding:8px 12px;background:#f6f8fa;border-radius:4px;">'
            '<div style="font-weight:600;">Extraction</div>' + "".join(body_bits) + "</div>"
        )

    archive_html = ""
    if stage == "ARCHIVED":
        for row in audit_rows or []:
            if row.get("event") == "archived":
                detail = row.get("detail")
                if isinstance(detail, str):
                    try:
                        detail = json.loads(detail)
                    except Exception:
                        detail = {"detail": detail}
                if isinstance(detail, dict):
                    path = detail.get("archive_path")
                    sha = detail.get("file_sha256")
                    archive_html = (
                        '<div style="margin:10px 0;">'
                        + (f'<div>Archived to <code>{_esc(path)}</code></div>' if path else "")
                        + (f'<div style="color:#57606a;">sha256 <code>{_esc(sha)}</code></div>' if sha else "")
                        + "</div>"
                    )
                break
    else:
        why = manifest.get("escalation_reason") or manifest.get("error_message") or ""
        archive_html = (
            f'<div style="margin:10px 0;color:#cf222e;">Not archived &mdash; stage {_esc(stage)}'
            + (f": {_esc(why)}" if why else "")
            + "</div>"
        )

    related_lines = []
    try:
        from pipeline.relations import context_block

        block = context_block(
            matter_id=manifest.get("matter_id"), doc_id=manifest.get("doc_id")
        )
        for line in block.splitlines()[1:]:  # drop the advisory banner line
            if line.strip():
                related_lines.append(f"<div>{_esc(line.strip(' -'))}</div>")
    except Exception:
        related_lines = []
    related_html = (
        '<div style="margin:10px 0;">'
        '<div style="font-weight:600;">Related (advisory)</div>' + "".join(related_lines) + "</div>"
        if related_lines
        else ""
    )

    timeline_rows = _processing_timeline(audit_rows)
    timeline_html = ""
    if timeline_rows:
        trows = "".join(
            f'<tr><td style="padding:1px 10px 1px 0;color:#57606a;">{_esc(ts)}</td>'
            f'<td style="padding:1px 0;">{_esc(event)}'
            + (f' <span style="color:#57606a;">{_esc(delta)}</span>' if delta else "")
            + "</td></tr>"
            for event, ts, delta in timeline_rows
        )
        timeline_html = (
            '<div style="margin:10px 0;">'
            '<div style="font-weight:600;">Processing timeline</div>'
            f'<table style="font-size:12px;">{trows}</table></div>'
        )
    narrative = _what_happened(manifest, audit_rows)
    what_html = ""
    if narrative:
        reason = str(manifest.get("escalation_reason") or manifest.get("error_message") or "")
        friendly = friendly_reason(reason) if stage in ("REVIEW", "FAILED") else None
        inner = f'<div>{_esc(narrative)}</div>'
        if friendly:
            inner += (
                f'<div style="margin-top:4px;"><strong>Why:</strong> {_esc(friendly[0])}</div>'
                f'<div style="margin-top:4px;"><strong>Next steps:</strong> {_esc(friendly[1])}</div>'
            )
        box_border = "#9a6700" if stage == "REVIEW" else ("#cf222e" if stage == "FAILED" else "#0969da")
        what_html = (
            '<div style="margin:10px 0;padding:8px 12px;background:#f6f8fa;'
            f'border-left:3px solid {box_border};border-radius:4px;">'
            '<div style="font-weight:600;">What happened</div>' + inner + "</div>"
        )

    n_events = len(audit_rows or [])
    chain_text = (
        "unavailable" if chain_valid is None else ("verified intact" if chain_valid else "BROKEN — investigate immediately")
    )
    chain_color = "#1a7f37" if chain_valid else "#cf222e"
    audit_html = (
        f'<div style="margin:10px 0;color:#57606a;">Audit chain ({n_events} events): '
        f'<span style="color:{chain_color};font-weight:600;">{chain_text}</span></div>'
    )

    trace_html = (
        f'<div style="color:#57606a;">trace_id: {_esc(manifest.get("trace_id"))}</div>'
        if manifest.get("trace_id")
        else ""
    )
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#1f2328;">
  <div style="padding:10px 14px;border-radius:6px;background:{status_color}1a;border:1px solid {status_color};">
    <span style="font-size:16px;font-weight:700;color:{status_color};">{status_emoji} {stage}</span>
    <span style="color:#57606a;"> &mdash; this document has completed mailroom processing</span>
  </div>
  <table style="margin:12px 0;font-size:13px;">{meta_html}</table>
  <div style="font-weight:600;">Classification</div>
  <table style="font-size:13px;">{classification}</table>
  {what_html}{timeline_html}{triage_html}{handoff_html}{extraction_html}{archive_html}{related_html}{audit_html}{trace_html}
  <div style="margin-top:14px;padding-top:10px;border-top:1px solid #d0d7de;color:#57606a;font-size:12px;">
    Processed by the LLM Mailroom agent &middot;
    <a href="https://github.com/Exios66/mailroom-dev" style="color:#0969da;">github.com/Exios66/mailroom-dev</a>
    &middot; {_esc(_now_iso())}
  </div>
</div>"""


def _load_audit_rows(doc_id: str) -> tuple[list[dict], bool | None]:
    """Audit chain rows + hash-chain verdict for the echo body (best-effort)."""
    try:
        import asyncio

        from storage.audit_log import get_audit_chain
        from schemas.audit import AuditLogEntry, verify_chain

        records = asyncio.run(get_audit_chain(doc_id))
        entries = [
            AuditLogEntry(
                entry_id=r["entry_id"],
                doc_id=doc_id,
                matter_id=r.get("matter_id") or "",
                event=r["event"],
                actor=r["actor"],
                detail=r["detail"],
                prev_hash=r["prev_hash"],
                entry_hash=r["entry_hash"],
                timestamp=r["timestamp"],
            )
            for r in records
        ]
        return records, verify_chain(entries)
    except Exception:
        logger.exception("gmail_echo_audit_read_failed", doc_id=doc_id)
        return [], None


def _retry_reaction(message_id: str) -> None:
    """Terminal-stage reaction retry (see send_intake_echo). Never raises."""
    try:
        if reactions_enabled():
            react_to_message(str(message_id))
    except Exception:
        logger.warning("gmail_echo_reaction_retry_failed", message_id=str(message_id), exc_info=True)


def _echo_gate(manifest: dict):
    """(doc_id, stage, message_id, sender, intake) when an echo applies, else None."""
    intake = (manifest or {}).get("intake") or {}
    doc_id = str((manifest or {}).get("doc_id") or "")
    stage = str((manifest or {}).get("stage") or "")
    message_id = intake.get("message_id")
    sender = intake.get("sender")
    if not (intake.get("source") == "gmail" and message_id and sender):
        return None
    if not echoes_enabled():
        return None
    return doc_id, stage, message_id, sender, intake


def _enqueue_echo(manifest: dict) -> str | None:
    """Build the echo and enqueue it durably. Returns its dedup key (None = no echo)."""
    from . import mail_outbox

    gate = _echo_gate(manifest)
    if gate is None:
        return None
    doc_id, stage, message_id, sender, intake = gate
    try:
        group_size = int(intake.get("group_size") or 0)
    except (TypeError, ValueError):
        group_size = 0
    if group_size > 1:
        handled, digest_key = _handle_group_result(manifest, gate)
        if handled:
            return digest_key
    dedup_key = f"echo:{doc_id}:{stage}"
    if mail_outbox.row_state(dedup_key) is not None:
        return dedup_key  # already queued/sent/dead: skip the audit read and build
    cfg = load_config()
    audit_rows, chain_valid = _load_audit_rows(doc_id)
    body = build_echo_body(manifest, audit_rows, chain_valid)
    html = build_echo_html(manifest, audit_rows, chain_valid)
    original_subject = str(intake.get("subject") or manifest.get("original_filename") or "mailroom intake")
    address = str(cfg.get("address") or "")
    domain = address.rpartition("@")[2].strip() if "@" in address else ""
    domain = domain or "mailroom.local"
    mail_outbox.enqueue(
        dedup_key,
        to_addr=str(sender),
        subject=f"Re: {original_subject}",
        text=body,
        html=html,
        headers={
            "In-Reply-To": str(message_id),
            "References": str(message_id),
            "Message-ID": f"<mailroom-echo-{doc_id or uuid.uuid4().hex[:12]}-{stage}@{domain}>",
            "Auto-Submitted": "auto-replied",
        },
    )
    return dedup_key


def _digest_summary(manifest: dict) -> dict:
    intake = manifest.get("intake") or {}
    triage = intake.get("triage") if isinstance(intake.get("triage"), dict) else {}
    debug = triage.get("debug") if isinstance(triage.get("debug"), dict) else {}
    return {
        "filename": manifest.get("original_filename"),
        "doc_type": manifest.get("doc_type"),
        "doc_subclass": manifest.get("doc_subclass"),
        "confidence": manifest.get("classification_confidence"),
        "reason": manifest.get("escalation_reason") or manifest.get("error_message"),
        "model": debug.get("model") if isinstance(debug.get("model"), str) else None,
    }


def _handle_group_result(manifest: dict, gate) -> tuple[bool, str | None]:
    """Bundle handling for one terminal manifest: ``(handled, digest_key)``.

    ``handled`` False → send the normal per-document echo (unknown group, or a
    late result for a group whose digest already went out without it).
    """
    from . import mail_outbox

    doc_id, stage, message_id, _sender, _intake = gate
    info = mail_outbox.group_info(str(message_id))
    if info is None:
        return False, None
    if info.get("closed_at") is not None:
        if mail_outbox.group_result_stage(str(message_id), doc_id) == stage:
            return True, None  # already in the digest
        return False, None
    have, expected = mail_outbox.record_group_result(
        str(message_id), doc_id, stage, _digest_summary(manifest)
    )
    if expected and have >= expected and mail_outbox.close_group(str(message_id)):
        return True, _enqueue_digest(str(message_id), incomplete=False)
    return True, None


def build_digest_text(subject: str, results: list[dict], expected: int, incomplete: bool) -> str:
    lines = ["MAILROOM BUNDLE REPORT", "=" * 60, ""]
    if incomplete:
        lines.append(
            f"INCOMPLETE: {len(results)} of {expected} documents finished; the rest are still "
            "processing or were parked. Each late document gets its own report."
        )
    else:
        lines.append(f"All {len(results)} documents from your email have finished processing.")
    lines.append("")
    for r in results:
        conf = r.get("confidence")
        lines.append(
            f"- {r.get('filename') or r.get('doc_id')}: {str(r.get('stage') or '').upper()}"
            f" | {r.get('doc_type') or 'n/a'}"
            + (f" / {r.get('doc_subclass')}" if r.get("doc_subclass") else "")
            + f" | confidence {conf if conf is not None else 'n/a'}"
        )
        if r.get("reason"):
            lines.append(f"    why: {r.get('reason')}")
        lines.append(f"    doc_id: {r.get('doc_id')}")
    models = sorted({str(r["model"]) for r in results if r.get("model")})
    lines += ["", f"answered by: {', '.join(models) if models else 'the full mailroom pipeline'}"]
    lines += ["", "Processed by the LLM Mailroom agent", "mailroom-dev: https://github.com/Exios66/mailroom-dev"]
    return "\n".join(lines)


def build_digest_html(subject: str, results: list[dict], expected: int, incomplete: bool) -> str:
    colors = {"ARCHIVED": "#1a7f37", "REVIEW": "#9a6700", "FAILED": "#cf222e"}
    rows = []
    for r in results:
        stage = str(r.get("stage") or "").upper()
        conf = r.get("confidence")
        reason = f'<div style="color:#57606a;">{_esc(r.get("reason"))}</div>' if r.get("reason") else ""
        rows.append(
            "<tr>"
            f'<td style="padding:4px 10px 4px 0;"><strong>{_esc(r.get("filename") or r.get("doc_id"))}</strong>{reason}</td>'
            f'<td style="padding:4px 10px 4px 0;">{_esc(r.get("doc_type") or "n/a")}'
            + (f" / {_esc(r.get('doc_subclass'))}" if r.get("doc_subclass") else "")
            + "</td>"
            f'<td style="padding:4px 10px 4px 0;">{_esc(conf if conf is not None else "n/a")}</td>'
            f'<td style="padding:4px 0;color:{colors.get(stage, "#57606a")};font-weight:600;">{_esc(stage)}</td>'
            "</tr>"
        )
    banner = (
        f"Incomplete &mdash; {len(results)} of {_esc(expected)} documents finished"
        if incomplete
        else f"All {len(results)} documents finished"
    )
    models = sorted({str(r["model"]) for r in results if r.get("model")})
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#1f2328;">'
        f'<div style="padding:10px 14px;border-radius:6px;background:#0969da1a;border:1px solid #0969da;">'
        f'<strong style="color:#0969da;">{banner}</strong></div>'
        '<table style="margin:12px 0;font-size:13px;border-collapse:collapse;">'
        '<tr style="color:#57606a;"><td>Document</td><td>Class</td><td>Confidence</td><td>Outcome</td></tr>'
        + "".join(rows)
        + "</table>"
        f'<div style="color:#57606a;font-size:12px;">Answered by: {_esc(", ".join(models) if models else "the full mailroom pipeline")}</div>'
        '<div style="margin-top:14px;color:#57606a;font-size:12px;">Processed by the LLM Mailroom agent &middot; '
        '<a href="https://github.com/Exios66/mailroom-dev" style="color:#0969da;">github.com/Exios66/mailroom-dev</a></div></div>'
    )


def _enqueue_digest(message_id: str, *, incomplete: bool) -> str | None:
    """Queue ``digest:<message_id>`` for a closed group. Returns its key or None."""
    from . import mail_outbox

    info = mail_outbox.group_info(message_id) or {}
    sender = str(info.get("sender") or "")
    if not sender:
        return None
    cfg = load_config()
    if not mail_guards.reply_allowed(sender, cfg.get("max_replies_per_hour")):
        logger.warning("gmail_reply_budget_exceeded", to=sender, kind="digest")
        return None
    results = mail_outbox.group_results(message_id)
    expected = int(info.get("expected") or len(results))
    subject = str(info.get("subject") or "")
    key = f"digest:{message_id}"
    mail_outbox.enqueue(
        key,
        to_addr=sender,
        subject=_reply_subject(subject),
        text=build_digest_text(subject, results, expected, incomplete),
        html=build_digest_html(subject, results, expected, incomplete),
        headers=_reply_headers(cfg, message_id, "digest"),
    )
    return key


def flush_stale_digests(now: float | None = None, max_age_s: float = 21600) -> list[str]:
    """Send a partial digest for bundles still open after ``max_age_s`` (worker sweep)."""
    from . import mail_outbox

    keys = []
    for message_id in mail_outbox.flush_stale_groups(now=now, max_age_s=max_age_s):
        try:
            key = _enqueue_digest(message_id, incomplete=True)
            if key:
                keys.append(key)
        except Exception:
            logger.exception("gmail_digest_flush_failed", message_id=message_id)
    return keys


def send_intake_echo(manifest: dict) -> bool:
    """Reply on the source Gmail thread with the terminal-stage report.

    Called at every terminal manifest (archived / review / failed). Enqueues
    the echo in the durable outbox (idempotent per ``echo:<doc>:<stage>``,
    across restarts), drains inline, and returns True only when the row is
    ``sent`` (also when it was already sent earlier). Failed sends are retried
    by the outbox worker with backoff. Never raises.
    """
    try:
        gate = _echo_gate(manifest)
        if gate is None:
            return False
        # Reaction guarantee (HUB-037): the claim-time reaction is best-effort
        # and a single-document triage-lane document has exactly ONE claim, so
        # retry it now (deduped per Message-ID; REACTIONS=0 respected).
        _retry_reaction(str(gate[2]))
        from . import mail_outbox

        dedup_key = _enqueue_echo(manifest)
        if dedup_key is None:
            return False
        if mail_outbox.row_state(dedup_key) != "sent":
            mail_outbox.drain_once()
        return mail_outbox.row_state(dedup_key) == "sent"
    except Exception:
        logger.warning("gmail_echo_failed", exc_info=True)
        return False


def _dispatch_echo_job(manifest: dict, message_id: str) -> None:
    """Thread target: build + enqueue the echo, wake the worker, retry the reaction."""
    try:
        _enqueue_echo(manifest)
        from . import mail_outbox

        worker = mail_outbox.get_worker()
        if worker is not None:
            worker.wake()
    except Exception:
        logger.exception("gmail_echo_build_failed", message_id=message_id)
    if reactions_enabled():
        _retry_reaction(message_id)


def dispatch_intake_echo(manifest) -> None:
    """Queue the completion echo off the document path (never raises).

    Starts one daemon thread (not joined) that builds the echo (audit read,
    chain verification, HTML), enqueues it durably and wakes the outbox worker,
    which does the sending. The caller never waits on I/O.
    """
    try:
        if not isinstance(manifest, dict):
            manifest = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else dict(manifest)
        gate = _echo_gate(manifest)
        if gate is None:
            return
        threading.Thread(
            target=_dispatch_echo_job,
            args=(manifest, str(gate[2])),
            name="gmail-echo-enqueue",
            daemon=True,
        ).start()
    except Exception:
        logger.exception("gmail_echo_dispatch_failed")
