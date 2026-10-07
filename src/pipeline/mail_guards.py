"""Gmail intake sender authentication and mail-loop guards.

Two concerns that decide whether an inbound message is acted on, and whether
the mailroom may ever reply to it:

- **Sender authentication.** The ``From`` header is trivially forged, so an
  allowlist alone is not a security control. Gmail stamps its own
  ``Authentication-Results`` header (authserv-id ``mx.google.com``) ABOVE any
  header the sender supplied, so only the topmost header is trusted; when it
  is not Gmail's the verdict fails closed (all ``None``).
- **Loop/backscatter guards.** Automated mail (auto-replies, bounces, mailing
  lists, our own messages) is never processed or answered, and replies to any
  one address are capped per hour via the durable outbox.
"""

from __future__ import annotations

import dataclasses
import email.message
import email.utils
import os
import re

DEFAULT_AUTHSERV_ID = "mx.google.com"
DEFAULT_MAX_REPLIES_PER_HOUR = 20

_AUTOMATED_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster", "no-reply", "noreply"})
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})
_METHOD_RE = re.compile(r"\b(dmarc|dkim|spf)\s*=\s*([A-Za-z]+)", re.IGNORECASE)


@dataclasses.dataclass
class AuthVerdict:
    dmarc: str | None = None
    dkim: str | None = None
    spf: str | None = None


def parse_authentication_results(
    values: list[str], authserv_id: str = DEFAULT_AUTHSERV_ID
) -> AuthVerdict:
    """Verdict from the TOPMOST ``Authentication-Results`` header only.

    ``values`` are in message order (topmost first). Lower headers, even ones
    claiming ``authserv_id``, may be sender-supplied forgeries and are never
    read. An untrusted or missing topmost header fails closed.
    """
    if not values:
        return AuthVerdict()
    top = " ".join(str(values[0]).split())  # unfold continuation lines
    first_token = top.split(";", 1)[0].strip().split()
    if not first_token or first_token[0].lower() != authserv_id.lower():
        return AuthVerdict()
    found: dict[str, str] = {}
    for method, result in _METHOD_RE.findall(top.split(";", 1)[1] if ";" in top else ""):
        found.setdefault(method.lower(), result.lower())
    return AuthVerdict(dmarc=found.get("dmarc"), dkim=found.get("dkim"), spf=found.get("spf"))


def message_auth_verdict(msg: email.message.Message, authserv_id: str = DEFAULT_AUTHSERV_ID) -> AuthVerdict:
    return parse_authentication_results(
        [str(v) for v in (msg.get_all("Authentication-Results") or [])], authserv_id
    )


def _bare_address(value: str | None) -> str:
    return email.utils.parseaddr(str(value or ""))[1].strip().lower()


def automated_reason(msg: email.message.Message, own_address: str) -> str | None:
    """Why ``msg`` is automated mail that must never be processed or answered, else None."""
    auto_submitted = str(msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return "auto-submitted"
    if str(msg.get("Precedence") or "").strip().lower() in _BULK_PRECEDENCE:
        return "precedence"
    if msg.get("List-Id") is not None or msg.get("List-Unsubscribe") is not None:
        return "list"
    if msg.get("X-Auto-Response-Suppress") is not None:
        return "auto-response-suppress"
    return_path = msg.get("Return-Path")
    if return_path is not None and str(return_path).strip() in ("<>", ""):
        return "null-return-path"
    if msg.get_content_type() == "multipart/report":
        return "dsn"
    sender = _bare_address(msg.get("From"))
    if sender.partition("@")[0] in _AUTOMATED_LOCAL_PARTS:
        return "daemon-sender"
    own = _bare_address(own_address)
    if own and sender == own:
        return "own-address"
    return None


def sender_permitted(
    allowed: set[str], sender: str, verdict: AuthVerdict, require_dmarc: bool
) -> tuple[bool, str]:
    """``(permitted, reason)`` for an inbound sender.

    Empty allowlist → open. Otherwise an exact, case-insensitive bare-address
    match (no plus-tag stripping: the allowlist is a security control) and,
    when ``require_dmarc``, a Gmail-stamped ``dmarc=pass``.
    """
    if not allowed:
        return True, "open"
    bare = _bare_address(sender)
    if bare not in {a.strip().lower() for a in allowed}:
        return False, "allowlist"
    if require_dmarc and verdict.dmarc != "pass":
        return False, "dmarc"
    return True, "allowlisted"


def require_dmarc() -> bool:
    return str(os.environ.get("MAILROOM_GMAIL_REQUIRE_DMARC", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def max_replies_per_hour() -> int:
    try:
        value = int(os.environ.get("MAILROOM_GMAIL_MAX_REPLIES_PER_HOUR", DEFAULT_MAX_REPLIES_PER_HOUR))
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPLIES_PER_HOUR
    return max(0, value)


def reply_allowed(to_addr: str, max_per_hour: int | None = None) -> bool:
    """Whether another reply to ``to_addr`` fits the hourly budget."""
    from . import mail_outbox

    budget = max_replies_per_hour() if max_per_hour is None else max_per_hour
    return mail_outbox.count_recent(_bare_address(to_addr) or to_addr, 3600.0) < budget
