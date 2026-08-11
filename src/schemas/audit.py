import hashlib
import json
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uuid

# A-4: hash payload version. v1 covered only {prev_hash, doc_id, entry_id,
# event, detail}; v2 adds the legally-relevant fields (matter_id, actor,
# timestamp) so those columns are no longer mutable without breaking the
# chain. Old rows (HASH_VERSION=1) stay verifiable via the v1 algorithm.
HASH_VERSION = 2

_HASH_FIELDS_V2 = ("prev_hash", "doc_id", "entry_id", "matter_id", "actor", "timestamp", "event", "detail")


class AuditLogEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    matter_id: str
    event: str
    actor: str
    detail: dict = Field(default_factory=dict)
    prev_hash: str = ""
    entry_hash: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def compute_audit_hash(
    prev_hash: str,
    doc_id: str,
    entry_id: str,
    event: str,
    detail: dict,
    matter_id: str = "",
    actor: str = "",
    timestamp=None,
) -> str:
    """Hash the entry payload. v2 covers the who/what/when fields (A-4) so an
    attacker cannot rewrite actor/matter_id/timestamp without breaking the
    chain. The `detail` dict is canonicalized (JSON, sorted keys) so dicts
    from different sources hash identically."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
    payload = json.dumps({
        "hash_version": HASH_VERSION,
        "prev_hash": prev_hash,
        "doc_id": doc_id,
        "entry_id": entry_id,
        "matter_id": matter_id,
        "actor": actor,
        "timestamp": ts,
        "event": event,
        "detail": detail,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_audit_hash_v1(prev_hash: str, doc_id: str, entry_id: str, event: str, detail: dict) -> str:
    """Legacy v1 payload — used only to verify pre-existing chains (rows
    written before HASH_VERSION 2). New entries always use v2."""
    payload = json.dumps({
        "prev_hash": prev_hash,
        "doc_id": doc_id,
        "entry_id": entry_id,
        "event": event,
        "detail": detail,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_audit_entry(
    doc_id: str,
    matter_id: str,
    event: str,
    actor: str,
    detail: dict,
    prev_hash: str = "",
) -> AuditLogEntry:
    entry = AuditLogEntry(
        doc_id=doc_id,
        matter_id=matter_id,
        event=event,
        actor=actor,
        detail=detail,
        prev_hash=prev_hash,
    )
    entry.entry_hash = compute_audit_hash(
        prev_hash, doc_id, entry.entry_id, event, detail,
        matter_id=matter_id, actor=actor, timestamp=entry.timestamp,
    )
    return entry


def verify_chain(entries: list[AuditLogEntry]) -> bool:
    """Verify a chain. v2 entries are checked with the v2 algorithm (their
    prev_hash links make the v2 fields tamper-evident); v1 entries (matter_id
    or actor empty) fall back to the v1 algorithm so old rows stay valid."""
    if not entries:
        return True
    entries_sorted = sorted(entries, key=lambda e: (e.timestamp, e.entry_id))
    for i, entry in enumerate(entries_sorted):
        expected_prev = "" if i == 0 else entries_sorted[i - 1].entry_hash
        if entry.prev_hash != expected_prev:
            return False
        if entry.matter_id or entry.actor:
            expected_hash = compute_audit_hash(
                entry.prev_hash, entry.doc_id, entry.entry_id, entry.event, entry.detail,
                matter_id=entry.matter_id, actor=entry.actor, timestamp=entry.timestamp,
            )
        else:
            # Legacy v1 row — verify with the v1 algorithm.
            expected_hash = compute_audit_hash_v1(
                entry.prev_hash, entry.doc_id, entry.entry_id, entry.event, entry.detail
            )
        if entry.entry_hash != expected_hash:
            return False
    return True
