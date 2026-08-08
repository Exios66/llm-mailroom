import pytest
from schemas.audit import (
    AuditLogEntry,
    build_audit_entry,
    compute_audit_hash,
    verify_chain,
)


class TestAuditLog:
    def test_build_audit_entry(self):
        entry = build_audit_entry(
            doc_id="doc-123",
            matter_id="matter-abc",
            event="classified",
            actor="sorter",
            detail={"doc_type": "contract", "confidence": 0.95},
            prev_hash="",
        )
        assert entry.doc_id == "doc-123"
        assert entry.event == "classified"
        assert entry.entry_hash != ""
        assert entry.prev_hash == ""

    def test_hash_chaining(self):
        entry1 = build_audit_entry(
            doc_id="doc-123",
            matter_id="matter-abc",
            event="classified",
            actor="sorter",
            detail={"type": "contract"},
            prev_hash="",
        )
        entry2 = build_audit_entry(
            doc_id="doc-123",
            matter_id="matter-abc",
            event="extracted",
            actor="contracts_specialist",
            detail={"confidence": 0.95},
            prev_hash=entry1.entry_hash,
        )
        assert entry2.prev_hash == entry1.entry_hash
        assert entry2.entry_hash != entry1.entry_hash

    def test_verify_chain_valid(self):
        entry1 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e1", actor="a1", detail={}, prev_hash=""
        )
        entry2 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e2", actor="a2", detail={},
            prev_hash=entry1.entry_hash,
        )
        entry3 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e3", actor="a3", detail={},
            prev_hash=entry2.entry_hash,
        )
        assert verify_chain([entry1, entry2, entry3]) is True

    def test_verify_chain_tampered(self):
        entry1 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e1", actor="a1", detail={}, prev_hash=""
        )
        entry2 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e2", actor="a2", detail={},
            prev_hash=entry1.entry_hash,
        )
        entry2.entry_hash = "tampered_hash_00000000000000000000000000"
        assert verify_chain([entry1, entry2]) is False

    def test_verify_chain_broken_link(self):
        entry1 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e1", actor="a1", detail={}, prev_hash=""
        )
        entry2 = build_audit_entry(
            doc_id="doc-1", matter_id="m-1", event="e2", actor="a2", detail={},
            prev_hash="wrong_previous_hash",
        )
        assert verify_chain([entry1, entry2]) is False

    def test_verify_chain_empty(self):
        assert verify_chain([]) is True

    def test_audit_entry_defaults(self):
        entry = AuditLogEntry(
            doc_id="d1",
            matter_id="m1",
            event="test",
            actor="tester",
            detail={},
        )
        assert entry.entry_id != ""
        assert entry.timestamp is not None

    def test_compute_audit_hash_deterministic(self):
        h1 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "val"})
        h2 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "val"})
        assert h1 == h2

    def test_compute_audit_hash_differs_on_input(self):
        h1 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "val"})
        h2 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "different"})
        assert h1 != h2
