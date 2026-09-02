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
}


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
        # RFC 6855: quoted strings are 7-bit unless UTF8=ACCEPT is enabled —
        # without this Gmail answers BAD "Could not parse command" on the
        # emoji label (the exact failure mode seen live).
        try:
            client.enable("UTF8=ACCEPT")
        except Exception:
            pass
        typ, _ = client.select(cfg["folder"], readonly=False)
        if typ != "OK":
            raise GmailIntakeError(f"cannot select folder {cfg['folder']!r}")
        # Best-effort label creation (Gmail auto-creates on STORE in most
        # cases; CREATE makes it deterministic). Already-exists errors ignored.
        try:
            client.create(f'"{label}"'.encode("utf-8"))
        except Exception:
            pass
        # Bytes args: imaplib encodes str args as ASCII — the emoji label
        # must ride through as pre-encoded UTF-8 bytes.
        typ, data = client.uid(
            "SEARCH", None, f'HEADER Message-ID "{message_id}"'.encode("utf-8")
        )
        uids = (data[0] or b"").split() if typ == "OK" and data else []
        for uid in uids:
            typ, _ = client.uid(
                "STORE",
                uid,
                "+X-GM-LABELS",
                f'"{label}"'.encode("utf-8"),
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
                queued = 0
                for filename, content in extract_attachments(msg):
                    meta = {
                        "matter_id": matter_id,
                        "source": "gmail",
                        "message_id": message_id,
                        "sender": sender,
                        "subject": subject[:200],
                        "received_at": _received_at(msg),
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
