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
        from datetime import datetime, timezone

        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        h1 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "val"}, matter_id="m1", actor="a1", timestamp=ts)
        h2 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "val"}, matter_id="m1", actor="a1", timestamp=ts)
        assert h1 == h2

    def test_hash_covers_actor_matter_timestamp(self):
        """A-4: the v2 payload covers the who/what/when fields — mutating them
        without re-hashing breaks the chain."""
        from datetime import datetime, timezone

        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        h1 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {}, matter_id="m1", actor="alice", timestamp=ts)
        h2 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {}, matter_id="m1", actor="mallory", timestamp=ts)
        h3 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {}, matter_id="m2", actor="alice", timestamp=ts)
        assert h1 != h2  # actor mutation breaks the hash
        assert h1 != h3  # matter_id mutation breaks the hash

    def test_v1_hash_still_verifies(self):
        """Legacy v1 rows (empty matter_id/actor) verify with the v1 algorithm."""
        from schemas.audit import compute_audit_hash_v1

        legacy = compute_audit_hash_v1("", "doc-1", "entry-1", "event", {"key": "val"})
        entry = AuditLogEntry(
            entry_id="entry-1",
            doc_id="doc-1", matter_id="", event="event", actor="", detail={"key": "val"},
            prev_hash="", entry_hash=legacy,
        )
        assert verify_chain([entry]) is True

    def test_compute_audit_hash_differs_on_input(self):
        h1 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "val"})
        h2 = compute_audit_hash("prev", "doc-1", "entry-1", "event", {"key": "different"})
        assert h1 != h2
