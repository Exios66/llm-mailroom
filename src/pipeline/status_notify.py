"""Watcher status notifications (HUB-050).

A dead process cannot email anyone, so the 🔴 DOWN alert is sent by the
EXTERNAL watchdog (`python -m pipeline.watchdog`); this module carries the
shared email plumbing for BOTH sides — concise, stylized status emails to
the human's inbox. Human directive 2026-09-04: **emails fire ONLY in
emergency scenarios and at watcher start/relaunch — never periodically.**

* ``running``    — 🟢 startup/relaunch confirmation (sent by the watcher).
* ``down``       — 🔴 heartbeat lost (sent by the watchdog).
* ``still_down`` — 🟠 reminder while the outage continues (watchdog).

Design laws: fail-soft (never raises into the caller), hermetic tests (own
SMTP factory seam + ``MAILROOM_WATCHER_STATUS=0`` kill-switch), and SMTP
settings reused from the gmail intake channel (same account the document
echoes send from).
"""
import email.message
import email.utils
import os
import smtplib
import socket
from datetime import datetime, timezone

import structlog

from .env import load_env

load_env()

logger = structlog.get_logger(__name__)

DEFAULT_STATUS_EMAIL = "axios337@gmail.com"

_KINDS = {
    "running": {"emoji": "🟢", "color": "#0f7b3a", "label": "RUNNING"},
    "down": {"emoji": "🔴", "color": "#b3261e", "label": "DOWN"},
    "still_down": {"emoji": "🟠", "color": "#b26a00", "label": "STILL DOWN"},
}

_FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif"

# Test/smoke seam: when set, EVERY SMTP access in this module goes through
# this factory instead of a real smtplib connection.
_INJECTED_SMTP_FACTORY = None


def set_status_smtp_factory(factory) -> None:
    """Inject an SMTP client factory (test/smoke seam). Pass None to reset."""
    global _INJECTED_SMTP_FACTORY
    _INJECTED_SMTP_FACTORY = factory


def status_enabled() -> bool:
    """Kill-switch (``MAILROOM_WATCHER_STATUS``): off disables every status
    email and the watcher-side heartbeat enrichment — hermetic tests set 0."""
    return str(os.environ.get("MAILROOM_WATCHER_STATUS", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def status_recipient() -> str:
    """The human's status inbox (``MAILROOM_STATUS_EMAIL``)."""
    return os.environ.get("MAILROOM_STATUS_EMAIL", DEFAULT_STATUS_EMAIL).strip()


def _smtp_config() -> dict | None:
    """Gmail channel SMTP settings (the same account the echoes send from)."""
    from .gmail_intake import load_config

    try:
        cfg = load_config()
    except Exception:
        return None
    if not (cfg.get("address") and cfg.get("password")):
        return None
    return cfg


def _render_html(kind: str, title: str, rows: list[tuple[str, str]], note: str | None) -> str:
    meta = _KINDS[kind]
    rows_html = "".join(
        '<tr>'
        f'<td style="padding:6px 14px 6px 0;color:#5f6368;white-space:nowrap;'
        f'font:600 13px {_FONT};vertical-align:top">{label}</td>'
        f'<td style="padding:6px 0;color:#202124;font:400 13px {_FONT};'
        f'word-break:break-word">{value}</td>'
        '</tr>'
        for label, value in rows
    )
    note_html = (
        f'<p style="margin:14px 0 0;color:#5f6368;font:400 13px/1.5 {_FONT}">{note}</p>'
        if note
        else ""
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f'<div style="max-width:560px;margin:0 auto;border:1px solid #e0e0e0;'
        f'border-radius:10px;overflow:hidden">'
        f'<div style="background:{meta["color"]};padding:14px 18px">'
        f'<span style="color:#ffffff;font:600 16px {_FONT}">{meta["emoji"]} {title}</span>'
        f'</div>'
        f'<div style="padding:14px 18px">'
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse">{rows_html}</table>'
        f'{note_html}</div>'
        f'<div style="padding:10px 18px;border-top:1px solid #eeeeef;color:#9aa0a6;'
        f'font:400 11px {_FONT}">LLM Mailroom watcher status · {socket.gethostname()} · {now}</div>'
        '</div>'
    )


def _render_text(title: str, rows: list[tuple[str, str]], note: str | None) -> str:
    lines = [title, ""]
    lines.extend(f"{label}: {value}" for label, value in rows)
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def send_status_email(
    kind: str, title: str, rows: list[tuple[str, str]], note: str | None = None
) -> bool:
    """Send one concise stylized status email. Fail-soft: logs and returns
    False on any failure — a status notification must never crash its host."""
    if kind not in _KINDS:
        raise ValueError(f"unknown status email kind: {kind}")
    if not status_enabled():
        return False
    cfg = _smtp_config()
    if cfg is None:
        logger.warning("status_email_skipped", kind=kind, reason="gmail channel not configured")
        return False
    recipient = status_recipient()
    if not recipient:
        return False
    meta = _KINDS[kind]
    ok = False
    client = None
    try:
        msg = email.message.EmailMessage()
        msg["From"] = cfg["address"]
        msg["To"] = recipient
        msg["Subject"] = (
            f'{meta["emoji"]} Mailroom watcher {meta["label"]} — {socket.gethostname()}'
        )
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = (
            f"<mailroom-status-{kind}-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}@mailroom.local>"
        )
        msg.set_content(_render_text(title, rows, note))
        msg.add_alternative(_render_html(kind, title, rows, note), subtype="html")

        factory = _INJECTED_SMTP_FACTORY or (
            lambda: smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
        )
        client = factory()
        client.login(cfg["address"], cfg["password"])
        client.sendmail(cfg["address"], [recipient], msg.as_bytes())
        ok = True
        logger.info("status_email_sent", kind=kind, to=recipient)
    except Exception as exc:
        logger.warning(
            "status_email_failed", kind=kind, to=recipient, error=f"{type(exc).__name__}: {exc}"
        )
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass
    return ok