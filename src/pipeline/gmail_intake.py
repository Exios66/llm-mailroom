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
  accept all).
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

import datetime
import email
import email.utils
import imaplib
import json
import os
import re
import smtplib
import threading
import time
import uuid

import structlog

from .env import load_env

logger = structlog.get_logger(__name__)

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_FOLDER = "INBOX"
DEFAULT_POLL_SECONDS = 60.0
DEFAULT_MAX_ATTACHMENT_MB = 50
IMAP_TIMEOUT_SECONDS = 30
_STATE_KEEP_MESSAGE_IDS = 2000
# The "check" reaction: an emoji-named Gmail label applied to the source
# message when the watcher picks the attachment up for processing (HUB-037).
DEFAULT_REACTION_LABEL = "✅"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465

# Matter routing: subject tag ``[M:<matter_id>]`` (watcher files the document
# under this matter via the inbox meta sidecar).
_MATTER_TAG_RE = re.compile(r"\[M:([A-Za-z0-9_.-]{1,64})\]")

_FILENAME_UNSAFE_RE = re.compile(r"[\x00-\x1f/]")


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
# connection for the same Message-ID.
_REACTION_ATTEMPTED: set[str] = set()
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


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text())
        if isinstance(data, dict):
            ids = data.get("processed_message_ids", [])
            return {"processed_message_ids": [str(i) for i in ids if i]}
    except Exception:
        pass
    return {"processed_message_ids": []}


def _save_state(state: dict) -> None:
    try:
        ids = state.get("processed_message_ids", [])[-_STATE_KEEP_MESSAGE_IDS:]
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(
            json.dumps({"processed_message_ids": ids, "updated_at": _now_iso()})
        )
    except Exception:
        logger.exception("gmail_intake_state_write_failed")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = _FILENAME_UNSAFE_RE.sub("_", name).strip().strip(".")
    return name or None


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


def deliver_attachment(filename: str, content: bytes, meta: dict) -> tuple[str | None, str | None]:
    """Write one attachment into the inbox + meta sidecar (the /upload route).

    Returns ``(delivered_filename, reject_reason)`` — reason is None on
    success, else ``"filename"`` | ``"extension"`` | ``"size"``. Collisions
    are uniquified exactly like ``/upload``.
    """
    from .bins import inbox_dir, write_inbox_meta, accepted_extensions

    safe = _safe_filename(filename)
    if safe is None:
        return None, "filename"
    if _extension_of(safe) not in accepted_extensions():
        return None, "extension"
    if len(content) > meta["_max_attachment_bytes"]:
        return None, "size"

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / safe
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = inbox / f"{stem}-{counter}{suffix}"
            counter += 1
    dest.write_bytes(content)
    write_inbox_meta(dest, **{k: v for k, v in meta.items() if not k.startswith("_")})
    return dest.name, None


def _fetch_message(client, uid: str) -> bytes | None:
    typ, data = client.uid("fetch", uid.encode(), "(RFC822)")
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


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
    cfg = config or load_config()
    report: dict = {
        "connected": False,
        "messages_seen": 0,
        "attachments_queued": 0,
        "skipped_extension": 0,
        "skipped_size": 0,
        "skipped_sender": 0,
        "skipped_no_attachments": 0,
        "marked_seen": 0,
        "already_processed": 0,
        "errors": [],
    }
    if not cfg.get("address") or not cfg.get("password"):
        report["errors"].append("missing_credentials")
        _record_status(last_error="missing_credentials", last_poll_at=_now_iso())
        return report

    state = _load_state()
    processed_ids: list[str] = state.get("processed_message_ids", [])

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

        for uid in uid_list:
            uid = uid.decode("ascii", errors="ignore")
            try:
                raw = _fetch_message(client, uid)
                if raw is None:
                    report["errors"].append(f"fetch_failed:{uid}")
                    continue
                msg = email.message_from_bytes(raw)
                message_id = _message_id(msg) or f"uid:{uid}"
                if message_id in processed_ids:
                    report["already_processed"] += 1
                    _mark_seen(client, uid)
                    continue

                sender = _sender_address(msg)
                if cfg["allowed_senders"] and sender not in cfg["allowed_senders"]:
                    logger.info("gmail_message_sender_rejected", sender=sender, uid=uid)
                    report["skipped_sender"] += 1
                    processed_ids.append(message_id)
                    _mark_seen(client, uid)
                    continue

                subject = str(msg.get("Subject") or "")
                matter_id = parse_matter_id(subject) or cfg["default_matter_id"]
                # Single vs bundle routing (HUB-037): count the attachments
                # that WOULD be delivered (extension + size guards) and route
                # the message — ONE accepted attachment = single-document
                # upload (free-triage lane); TWO OR MORE = multi-document
                # upload (full paid pipeline, triage dropped).
                from .bins import accepted_extensions

                accepted = []
                for filename, content in extract_attachments(msg):
                    if _extension_of(filename) not in accepted_extensions():
                        report["skipped_extension"] += 1
                        continue
                    if len(content) > cfg["max_attachment_bytes"]:
                        report["skipped_size"] += 1
                        continue
                    accepted.append((filename, content))
                route = "triage" if len(accepted) == 1 else "pipeline"
                queued = 0
                for filename, content in accepted:
                    meta = {
                        "matter_id": matter_id,
                        "source": "gmail",
                        "message_id": message_id,
                        "sender": sender,
                        "subject": subject[:200],
                        "received_at": _received_at(msg),
                        "route": route,
                        "upload_id": uuid.uuid4().hex[:12],
                        "size": len(content),
                        "original_filename": filename,
                        "_max_attachment_bytes": cfg["max_attachment_bytes"],
                    }
                    delivered, reject_reason = deliver_attachment(filename, content, meta)
                    if delivered is None:
                        if reject_reason == "extension":
                            report["skipped_extension"] += 1
                        elif reject_reason == "size":
                            report["skipped_size"] += 1
                        continue
                    queued += 1
                    logger.info(
                        "gmail_attachment_queued",
                        file=delivered,
                        matter_id=matter_id,
                        sender=sender,
                        message_id=message_id,
                        route=route,
                    )
                report["attachments_queued"] += queued
                if queued == 0:
                    report["skipped_no_attachments"] += 1
                    logger.info(
                        "gmail_message_no_processable_attachments",
                        message_id=message_id,
                        sender=sender,
                        subject=subject[:120],
                        detail="no accepted-extension attachment — message marked seen only",
                    )
                processed_ids.append(message_id)
                if _mark_seen(client, uid):
                    report["marked_seen"] += 1
            except Exception as exc:  # one bad message must not stop the sweep
                logger.exception("gmail_message_failed", uid=uid)
                report["errors"].append(f"message_failed:{uid}:{type(exc).__name__}")
    except Exception as exc:
        logger.exception("gmail_poll_failed")
        report["errors"].append(f"poll_failed:{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

    _save_state({"processed_message_ids": processed_ids})
    _record_status(
        last_poll_at=_now_iso(),
        last_error=report["errors"][0] if report["errors"] else None,
        messages_seen=_STATUS["messages_seen"] + report["messages_seen"],
        attachments_queued=_STATUS["attachments_queued"] + report["attachments_queued"],
    )
    if report["errors"]:
        logger.warning("gmail_poll_report", **report)
    return report


class GmailIntakePoller(threading.Thread):
    """Background poll loop; daemon thread, one sweep per ``poll_seconds``."""

    def __init__(self, poll_seconds: float | None = None):
        super().__init__(name="gmail-intake", daemon=True)
        cfg = load_config()
        self.poll_seconds = poll_seconds or cfg["poll_seconds"]
        self._stop_event = threading.Event()

    def run(self) -> None:
        _record_status(running=True, enabled=True)
        logger.info("gmail_poller_started", poll_seconds=self.poll_seconds)
        try:
            while not self._stop_event.is_set():
                poll_once()
                self._stop_event.wait(self.poll_seconds)
        finally:
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

_ECHO_DONE: set[tuple[str, str]] = set()
_ECHO_LOCK = threading.Lock()


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
        logger.debug("gmail_echo_related_section_failed")

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


def send_intake_echo(manifest: dict) -> bool:
    """Reply on the source Gmail thread with the terminal-stage report.

    Called by the graph at every terminal manifest (archived / review /
    failed). Best-effort: never raises, retried by a later terminal event of
    the same document if the send fails. Returns True when sent.
    """
    intake = (manifest or {}).get("intake") or {}
    doc_id = str((manifest or {}).get("doc_id") or "")
    stage = str((manifest or {}).get("stage") or "")
    message_id = intake.get("message_id")
    sender = intake.get("sender")
    if not (intake.get("source") == "gmail" and message_id and sender):
        return False
    if not echoes_enabled():
        return False
    # Reaction guarantee (HUB-037): the claim-time ✅ reaction is best-effort
    # and a failed attempt is only retried on a LATER claim — but a
    # single-document triage-lane document has exactly ONE claim, so a
    # claim-time failure would leave the sender without the "picked up"
    # acknowledgement forever. Retry the reaction now that the document has
    # reached a terminal stage. Deduped per Message-ID: a reaction that
    # already succeeded (or is still in flight) is never re-sent, and
    # MAILROOM_GMAIL_REACTIONS=0 stays respected. Best-effort — a failed
    # retry must never block the completion echo.
    try:
        if reactions_enabled():
            react_to_message(str(message_id))
    except Exception:
        logger.warning("gmail_echo_reaction_retry_failed", message_id=str(message_id), exc_info=True)
    dedup_key = (doc_id, stage)
    with _ECHO_LOCK:
        if dedup_key in _ECHO_DONE:
            return True
        _ECHO_DONE.add(dedup_key)

    ok = False
    client = None
    try:
        cfg = load_config()
        audit_rows, chain_valid = _load_audit_rows(doc_id)
        body = build_echo_body(manifest, audit_rows, chain_valid)
        html = build_echo_html(manifest, audit_rows, chain_valid)

        msg = email.message.EmailMessage()
        msg["From"] = cfg["address"]
        msg["To"] = str(sender)
        original_subject = str(intake.get("subject") or manifest.get("original_filename") or "mailroom intake")
        msg["Subject"] = f"Re: {original_subject}"
        msg["In-Reply-To"] = str(message_id)
        msg["References"] = str(message_id)
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = f"<mailroom-echo-{doc_id or uuid.uuid4().hex[:12]}-{stage}@mailroom.local>"
        msg.set_content(body)  # text/plain first (preferred fallback)
        msg.add_alternative(html, subtype="html")  # clean rendering in Gmail

        factory = _INJECTED_SMTP_FACTORY or (
            lambda: smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=IMAP_TIMEOUT_SECONDS)
        )
        client = factory()
        client.login(cfg["address"], cfg["password"])
        client.sendmail(cfg["address"], [str(sender)], msg.as_bytes())
        ok = True
        _record_status(echoes_sent=_STATUS["echoes_sent"] + 1)
        logger.info(
            "gmail_echo_sent",
            doc_id=doc_id,
            stage=stage,
            to=sender,
            message_id=message_id,
        )
    except Exception as exc:
        logger.warning(
            "gmail_echo_failed",
            doc_id=doc_id,
            stage=stage,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass
    if not ok:
        # A failed echo must be retried by a later terminal event.
        with _ECHO_LOCK:
            _ECHO_DONE.discard(dedup_key)
    return ok


def dispatch_intake_echo(manifest) -> None:
    """Fire the completion echo off the document path (daemon thread, never raises)."""
    try:
        if not isinstance(manifest, dict):
            manifest = manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else dict(manifest)
        intake = manifest.get("intake") or {}
        if intake.get("source") != "gmail" or not echoes_enabled():
            return
        threading.Thread(
            target=send_intake_echo,
            args=(manifest,),
            name="gmail-echo",
            daemon=True,
        ).start()
    except Exception:
        logger.exception("gmail_echo_dispatch_failed")
