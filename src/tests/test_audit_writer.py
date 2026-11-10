"""Roadmap M3: the unified audit row and addendum writers (spec §4, §5.3)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog

from storage import db


@pytest.fixture
def audit_db(tmp_path, monkeypatch) -> Path:
    """A fresh SQLite database whose schema (and audit_meta) is in place."""
    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    return path


def _q(path: Path, sql: str, *args) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def _attempt(n: int, stage: str = "extraction", trigger: str = "initial", **detail):
    from audit.models import AttemptRow

    return AttemptRow(
        attempt_id=f"att-{stage}-{n}-{detail.pop('tag', '')}",
        stage=stage,
        attempt_no=n,
        trigger=trigger,
        outcome="ok",
        started_at=datetime(2026, 11, 3, 9, 0, n, tzinfo=timezone.utc),
        ended_at=datetime(2026, 11, 3, 9, 0, n + 1, tzinfo=timezone.utc),
        detail=detail or {"extracted_data": {"n": n}},
    )


def _row(doc_id: str, event: str = "archived", attempts=None, **detail):
    from audit.models import TerminalRow

    return TerminalRow(
        doc_id=doc_id,
        matter_id="M-1",
        event=event,
        actor="archivist",
        detail={"row_schema": 1, "stage": "archive", "pipeline_success": True, **detail},
        attempts=list(attempts or []),
    )


def _main_chain(path: Path) -> list[tuple]:
    return _q(path, "SELECT seq, entry_id, prev_hash, entry_hash FROM audit_log ORDER BY seq")


async def _stored_row_recomputes(doc_id: str) -> bool:
    from audit.writer import get_row, hash_entry

    row = await get_row(doc_id)
    return hash_entry(
        prev_hash=row["prev_hash"], doc_id=row["doc_id"], entry_id=row["entry_id"],
        event=row["event"], detail=row["detail"], matter_id=row["matter_id"],
        actor=row["actor"], timestamp=row["timestamp"],
    ) == row["entry_hash"]


async def test_row_hash_matches_dojo_format_audit_entry(audit_db):
    """Hash version 2 is byte-identical with the dojo in both directions.

    ``format_audit_entry`` fills the dojo's archive detail template, so its
    ``detail`` differs from ours; what must agree is the hash over whatever
    detail an entry carries. The dojo's ``archive_entry_hash`` (which
    ``format_audit_entry`` uses) reproduces the stored row's hash, and our
    ``hash_entry`` reproduces ``format_audit_entry``'s own output.
    """
    from llm_dojo_scoring import archive_entry_hash, format_audit_entry

    from audit.writer import get_row, hash_entry, write_terminal_row

    await write_terminal_row(_row("doc-0"))
    res = await write_terminal_row(_row("doc-a", doc_type="contract", nodes_visited=["intake", "archive"]))
    row = await get_row("doc-a")
    assert row["prev_hash"]  # a linked row, not the chain head
    fields = {k: row[k] for k in ("prev_hash", "doc_id", "entry_id", "matter_id", "actor", "timestamp", "event")}
    assert archive_entry_hash(**fields, detail=row["detail"]) == res.entry_hash == row["entry_hash"]

    dojo = format_audit_entry(**fields, detail=row["detail"], scoring={"overall_score": 0.5})
    assert hash_entry(**fields, detail=dojo["detail"]) == dojo["entry_hash"]
    assert await _stored_row_recomputes("doc-a")


async def test_rows_chain_globally_by_seq(audit_db):
    from audit.writer import write_terminal_row

    results = [await write_terminal_row(_row(f"doc-{i}")) for i in range(3)]
    chain = _main_chain(audit_db)
    assert [c[0] for c in chain] == [1, 2, 3]
    assert [r.seq for r in results] == [1, 2, 3]
    assert chain[0][2] == ""
    assert chain[1][2] == chain[0][3] and chain[2][2] == chain[1][3]
    assert all(r.kind == "row" for r in results)


async def test_subrow_hashes_listed_in_parent_detail_attempts(audit_db):
    from audit.writer import attempt_hash, get_row, write_terminal_row

    res = await write_terminal_row(
        _row("doc-s", attempts=[_attempt(1), _attempt(2, trigger="retry"), _attempt(1, stage="judge")])
    )
    row = await get_row("doc-s")
    listed = row["detail"]["attempts"]
    assert [a["attempt_no"] for a in listed] == [1, 2, 1]
    subs = {s["attempt_id"]: s for s in row["sub_rows"]}
    assert len(subs) == 3
    for item in listed:
        sub = subs[item["attempt_id"]]
        assert sub["parent_id"] == res.entry_id
        assert sub["entry_hash"] == item["entry_hash"] == attempt_hash(sub, matter_id="M-1", actor="archivist")
        assert item["stage"] == sub["stage"]
    assert await _stored_row_recomputes("doc-s")


async def test_subrows_and_row_commit_atomically(audit_db, monkeypatch):
    from audit import writer

    from sqlalchemy.orm import Session

    real_add = Session.add

    def boom(session, instance, **kwargs):
        if isinstance(instance, writer.AuditLogRecord):
            assert sum(isinstance(row, writer.AuditAttemptRecord) for row in session.identity_map.values()) == 2
            raise RuntimeError("injected after the sub-row insert")
        return real_add(session, instance, **kwargs)

    monkeypatch.setattr(Session, "add", boom)
    with pytest.raises(RuntimeError, match="injected"):
        await writer.write_terminal_row(_row("doc-x", attempts=[_attempt(1), _attempt(2)]))
    assert _q(audit_db, "SELECT count(*) FROM audit_attempts") == [(0,)]
    assert _q(audit_db, "SELECT count(*) FROM audit_log") == [(0,)]


async def test_second_terminal_write_becomes_resumed_addendum(audit_db):
    from audit.writer import get_row, write_terminal_row

    first = await write_terminal_row(_row("doc-r", event="failed", attempts=[_attempt(1)]))
    second = await write_terminal_row(_row("doc-r", attempts=[_attempt(1, trigger="resume", tag="r")]))
    assert second.kind == "addendum"
    assert _q(audit_db, "SELECT count(*) FROM audit_log") == [(1,)]
    adds = _q(audit_db, "SELECT addendum_id, kind, seq, prev_hash, entry_hash, detail FROM audit_addenda")
    assert len(adds) == 1
    addendum_id, kind, seq, prev_hash, entry_hash, _detail = adds[0]
    assert (addendum_id, kind, seq) == (second.entry_id, "resumed", 1)
    assert prev_hash == first.entry_hash and entry_hash == second.entry_hash
    parents = dict(_q(audit_db, "SELECT attempt_id, parent_id FROM audit_attempts"))
    assert parents == {"att-extraction-1-": first.entry_id, "att-extraction-1-r": second.entry_id}
    row = await get_row("doc-r")
    assert row["event"] == "failed"  # the row itself never changes
    assert [a["kind"] for a in row["addenda"]] == ["resumed"]
    assert row["addenda"][0]["detail"]["event"] == "archived"


async def test_seq_conflict_retries(audit_db, monkeypatch):
    """A writer whose tail read went stale hits the unique ``seq`` index and retries.

    SQLite's ``BEGIN IMMEDIATE`` already serializes writers across processes, so
    the race is reproduced by handing the first attempt a stale tail (what a
    writer sees when its lock did not serialize it, review item A17).
    """
    from audit import writer

    await writer.write_terminal_row(_row("doc-1"))
    real_tail = writer._read_tail
    calls = {"n": 0}

    async def stale_then_real(session):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_tail(session)

    monkeypatch.setattr(writer, "_read_tail", stale_then_real)
    with structlog.testing.capture_logs() as logs:
        res = await writer.write_terminal_row(_row("doc-2"))
    assert calls["n"] == 2
    assert any(e["event"] == "audit_write_conflict_retry" for e in logs)
    chain = _main_chain(audit_db)
    assert [c[0] for c in chain] == [1, 2]
    assert chain[1][2] == chain[0][3] and res.seq == 2
    assert await _stored_row_recomputes("doc-2")


async def test_seq_conflict_gives_up_after_bounded_retries(audit_db, monkeypatch):
    from audit import writer
    from audit.models import AuditWriteError

    await writer.write_terminal_row(_row("doc-1"))

    async def always_stale(session):
        return None

    monkeypatch.setattr(writer, "_read_tail", always_stale)
    with pytest.raises(AuditWriteError, match="seq"):
        await writer.write_terminal_row(_row("doc-2"))
    assert _q(audit_db, "SELECT count(*) FROM audit_log") == [(1,)]


async def test_addendum_chain_starts_at_row_hash(audit_db):
    from audit.models import Addendum
    from audit.writer import get_row, write_addendum, write_terminal_row

    row = await write_terminal_row(_row("doc-a"))
    a1 = await write_addendum(Addendum("doc-a", "review_decision", "reviewer", {"decision": "approve"}))
    a2 = await write_addendum(Addendum("doc-a", "rescore", "rescore-cli", {"scoring": {"overall_score": 1.0}}))
    adds = _q(audit_db, "SELECT seq, addendum_id, prev_hash, entry_hash FROM audit_addenda ORDER BY seq")
    assert adds == [(1, a1.entry_id, row.entry_hash, a1.entry_hash), (2, a2.entry_id, a1.entry_hash, a2.entry_hash)]
    assert (a1.seq, a2.seq) == (1, 2)
    stored = await get_row("doc-a")
    assert [a["kind"] for a in stored["addenda"]] == ["review_decision", "rescore"]
    # Addenda never move the global chain.
    assert [c[0] for c in _main_chain(audit_db)] == [1]


async def test_addendum_without_row_raises_NoAuditRow(audit_db):
    from audit.models import Addendum, NoAuditRow
    from audit.writer import write_addendum

    with pytest.raises(NoAuditRow):
        await write_addendum(Addendum("doc-none", "relations_linked", "relations", {}))
    assert _q(audit_db, "SELECT count(*) FROM audit_addenda") == [(0,)]


async def test_unknown_addendum_kind_rejected(audit_db):
    from audit.models import Addendum
    from audit.writer import write_addendum, write_terminal_row

    await write_terminal_row(_row("doc-a"))
    with pytest.raises(ValueError, match="kind"):
        await write_addendum(Addendum("doc-a", "note", "someone", {}))


async def test_concurrent_documents_do_not_fork_chain(audit_db):
    from audit.writer import write_terminal_row

    results = await asyncio.gather(
        *(write_terminal_row(_row(f"doc-{i:02d}", attempts=[_attempt(1, tag=str(i))])) for i in range(20))
    )
    chain = _main_chain(audit_db)
    assert [c[0] for c in chain] == list(range(1, 21))
    assert sorted(r.seq for r in results) == list(range(1, 21))
    prev = ""
    for _seq, _eid, prev_hash, entry_hash in chain:
        assert prev_hash == prev
        prev = entry_hash
    assert len({c[2] for c in chain}) == 20  # no two rows share a prev_hash
    assert _q(audit_db, "SELECT count(*) FROM audit_attempts") == [(20,)]
    for i in range(20):
        assert await _stored_row_recomputes(f"doc-{i:02d}")


async def test_unknown_event_rejected(audit_db):
    from audit.writer import write_terminal_row

    with pytest.raises(ValueError, match="event"):
        await write_terminal_row(_row("doc-a", event="classified"))
    assert _q(audit_db, "SELECT count(*) FROM audit_log") == [(0,)]


def test_unknown_attempt_stage_and_trigger_rejected():
    with pytest.raises(ValueError, match="stage"):
        _attempt(1, stage="intake")
    with pytest.raises(ValueError, match="trigger"):
        _attempt(1, trigger="manual")


async def test_writer_refuses_while_migration_pending(audit_db):
    from audit.models import Addendum, AuditMigrationPending
    from audit.writer import write_addendum, write_terminal_row

    await write_terminal_row(_row("doc-a"))
    with sqlite3.connect(audit_db) as conn:
        conn.execute("DELETE FROM audit_meta")
    with pytest.raises(AuditMigrationPending):
        await write_terminal_row(_row("doc-b"))
    with pytest.raises(AuditMigrationPending):
        await write_addendum(Addendum("doc-a", "rescore", "rescore-cli", {}))
    assert _q(audit_db, "SELECT doc_id FROM audit_log") == [("doc-a",)]
    assert _q(audit_db, "SELECT count(*) FROM audit_addenda") == [(0,)]


async def test_row_creates_catalog_parent_for_fk(audit_db):
    from audit.writer import write_terminal_row

    await write_terminal_row(_row("doc-new", original_filename="a.pdf"))
    assert _q(audit_db, "SELECT doc_id, matter_id FROM documents") == [("doc-new", "M-1")]


async def test_get_row_none_for_unknown_doc(audit_db):
    from audit.writer import get_row

    assert await get_row("nope") is None


async def test_unknown_attempt_start_time_can_be_written(audit_db):
    from audit.models import Addendum
    from audit.writer import attempt_hash, get_row, write_addendum, write_terminal_row

    first = _attempt(1)
    first.started_at = None
    await write_terminal_row(_row("doc-unknown", attempts=[first]))
    second = _attempt(2)
    second.started_at = None
    await write_addendum(Addendum("doc-unknown", "resumed", "archivist", {}, attempts=[second]))
    row = await get_row("doc-unknown")
    for sub in [*row["sub_rows"], *row["addenda"][0]["sub_rows"]]:
        assert sub["started_at"] is None
        assert sub["entry_hash"] == attempt_hash(sub, matter_id="M-1", actor="archivist")
    assert await _stored_row_recomputes("doc-unknown")


def test_hash_entry_preserves_empty_timestamp_and_rejects_invalid_timestamp():
    from audit.writer import hash_entry
    from schemas.audit import compute_audit_hash

    fields = dict(prev_hash="", doc_id="d", entry_id="a", event="attempt:judge",
                  detail={}, matter_id="m", actor="a", timestamp="")
    assert hash_entry(**fields) == compute_audit_hash(**fields)
    fields["timestamp"] = "invalid"
    with pytest.raises(ValueError):
        hash_entry(**fields)


async def test_attempt_stored_empty_started_at_round_trips(audit_db):
    """PR #160 review: an attempt sub-row whose stored started_at is "" (the
    documented unknown-start shape) must hash and verify, not raise."""
    from audit.writer import attempt_hash, get_row, write_terminal_row

    first = _attempt(1)
    first.started_at = None
    await write_terminal_row(_row("doc-empty-start", attempts=[first]))
    row = await get_row("doc-empty-start")
    for sub in row["sub_rows"]:
        sub["started_at"] = ""
        assert sub["entry_hash"] == attempt_hash(sub, matter_id="M-1", actor="archivist")


def test_document_lock_key_never_collides_with_the_chain_lock():
    from audit.writer import CHAIN_LOCK
    from storage.audit_log import doc_lock_key

    assert doc_lock_key("chain") != CHAIN_LOCK
    assert doc_lock_key("doc-1") == "audit_log:doc:doc-1"


async def test_addendum_without_attempts_drops_caller_supplied_attempt_refs(audit_db):
    from audit.models import Addendum
    from audit.writer import get_row, write_addendum, write_terminal_row

    await write_terminal_row(_row("doc-i"))
    forged = [{"attempt_id": "att-x", "entry_hash": "0" * 64}]
    await write_addendum(Addendum("doc-i", "review_decision", "reviewer", {"decision": "approve", "attempts": forged}))
    (add,) = (await get_row("doc-i"))["addenda"]
    assert add["detail"] == {"decision": "approve"}


def test_sub_row_detail_is_a_deep_snapshot():
    from audit.writer import _build_sub_rows

    att = _attempt(1)
    (rec,), _ = _build_sub_rows([att], parent_id="row-1", doc_id="doc-c", matter_id="M-1", actor="a")
    att.detail["extracted_data"]["n"] = 99
    assert rec.detail == {"extracted_data": {"n": 1}}
