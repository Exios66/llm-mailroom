import hashlib
import json
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import uuid


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


def compute_audit_hash(prev_hash: str, doc_id: str, entry_id: str, event: str, detail: dict) -> str:
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
        prev_hash, doc_id, entry.entry_id, event, detail
    )
    return entry


def verify_chain(entries: list[AuditLogEntry]) -> bool:
    if not entries:
        return True
    entries_sorted = sorted(entries, key=lambda e: e.timestamp)
    for i, entry in enumerate(entries_sorted):
        expected_prev = "" if i == 0 else entries_sorted[i - 1].entry_hash
        if entry.prev_hash != expected_prev:
            return False
        expected_hash = compute_audit_hash(
            entry.prev_hash, entry.doc_id, entry.entry_id, entry.event, entry.detail
        )
        if entry.entry_hash != expected_hash:
            return False
    return True
