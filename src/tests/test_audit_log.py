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


def _stored_chain_entries(doc_id: str):
    import asyncio

    from storage.audit_log import get_audit_chain

    return [
        AuditLogEntry(
            entry_id=r["entry_id"], doc_id=doc_id, matter_id=r["matter_id"],
            event=r["event"], actor=r["actor"], detail=r["detail"],
            prev_hash=r["prev_hash"], entry_hash=r["entry_hash"], timestamp=r["timestamp"],
        )
        for r in asyncio.run(get_audit_chain(doc_id))
    ]


def test_concurrent_appends_keep_chain_valid(temp_base_dir):
    """Concurrent appenders (API tasks on one loop + graph/watcher threads on
    their own loops) each read the latest hash, then write. The append must
    re-link under a write lock so the stored chain never forks."""
    import asyncio
    import threading

    from storage.audit_log import get_latest_audit_hash, write_audit_entry

    async def _append(i: int):
        prev = await get_latest_audit_hash("race-doc")
        await asyncio.sleep(0)  # let the other appenders read the same tail
        entry = build_audit_entry("race-doc", "M", "review_recorded", "human_reviewer", {"i": i}, prev_hash=prev)
        await write_audit_entry(entry)

    async def _burst(offset: int):
        await asyncio.gather(*[_append(offset + i) for i in range(4)])

    threads = [threading.Thread(target=lambda o=o: asyncio.run(_burst(o))) for o in (0, 10)]
    for t in threads:
        t.start()
    asyncio.run(_burst(20))
    for t in threads:
        t.join()

    entries = _stored_chain_entries("race-doc")
    assert len(entries) == 12
    assert len({e.prev_hash for e in entries}) == 12
    assert verify_chain(entries) is True

    import asyncio as _a
    from storage.audit_log import get_audit_chain

    seqs = [r["seq"] for r in _a.run(get_audit_chain("race-doc"))]
    assert seqs == list(range(1, 13))


async def test_ensure_schema_creates_postgres_schema_inside_running_loop(monkeypatch):
    """ensure_schema() is called from async code (API handlers, audit writes).
    With a non-SQLite URL it used asyncio.run(), which raises inside a running
    loop — the error was swallowed and the Postgres schema never created."""
    import threading

    from storage import db

    url = "postgresql+asyncpg://user:pw@db.invalid:5432/mailroom"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    calls = []

    async def _fake_create_schema(target_url):
        calls.append((target_url, threading.get_ident()))

    monkeypatch.setattr(db, "_create_schema_async", _fake_create_schema)

    assert db.ensure_schema() is True
    assert [c[0] for c in calls] == [url]
    assert db._schema_checked_url == url
    assert db.ensure_schema() is True  # cached — no second create
    assert len(calls) == 1


def test_ensure_schema_creates_postgres_schema_without_loop(monkeypatch):
    import asyncio

    from storage import db

    url = "postgresql+asyncpg://user:pw@db.invalid:5432/mailroom"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    calls = []

    async def _fake_create_schema(target_url):
        calls.append(target_url)

    monkeypatch.setattr(db, "_create_schema_async", _fake_create_schema)
    assert db.ensure_schema() is True
    assert calls == [url]


async def test_event_writer_holds_document_lock_through_commit_without_exclusive_chain_lock(monkeypatch):
    """The Postgres chain gate is taken shared (via execute); an exclusive
    acquire of the same key deadlocks concurrent writers (PR #160 review)."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from storage import audit_log

    session = MagicMock()
    session.get_bind.return_value.dialect.name = "postgresql"
    result = MagicMock()
    result.first.return_value = None
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    held = []

    async def lock(session_arg, key):
        assert session_arg is session
        held.append(key)

    async def model(session_arg):
        assert held == ["audit_doc:doc"]
        return audit_log.AuditLegacyRecord

    async def commit():
        assert held == ["audit_doc:doc"]
        assert isinstance(session.add.call_args.args[0], audit_log.AuditLegacyRecord)
        held.clear()

    session.commit = AsyncMock(side_effect=commit)

    @asynccontextmanager
    async def session_scope():
        yield session

    monkeypatch.setattr(audit_log, "ensure_schema", lambda: None)
    monkeypatch.setattr(audit_log, "async_session", session_scope)
    monkeypatch.setattr(audit_log, "acquire_write_lock", lock)
    monkeypatch.setattr(audit_log, "event_log_model", model)
    entry = build_audit_entry("doc", "matter", "classified", "sorter", {})
    await audit_log.write_audit_entry(entry)
    session.commit.assert_awaited_once()
    assert held == []
    assert any("pg_advisory_xact_lock_shared" in str(call.args[0])
               for call in session.execute.await_args_list)


async def test_default_schema_is_legacy_compatible_until_explicit_migration(tmp_path, monkeypatch):
    """Default startup must NOT install the unified shape (PR #160 safety gate).

    A fresh database keeps the current per-event ``audit_log``: many rows per
    document and a per-document ``seq``. If ``create_all`` installed the
    unified unique ``doc_id``/``seq`` constraints (and the startup migration
    were off), the second per-event write would fail. Only the explicit
    migration installs the unique indexes, after copying the rows out.
    """
    import sqlite3

    from storage import audit_migrate, db
    from storage.audit_log import get_audit_chain, get_latest_audit_hash, write_audit_entry

    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()

    for doc_id in ("doc-a", "doc-b"):
        prev = ""
        for event in ("classified", "extracted"):
            entry = build_audit_entry(
                doc_id, "matter-1", event, "test", {"doc": doc_id}, prev_hash=prev
            )
            await write_audit_entry(entry)
            prev = await get_latest_audit_hash(doc_id)

    def _unique(column: str) -> bool | None:
        with sqlite3.connect(path) as conn:
            for _seq, name, unique, *_ in conn.execute("PRAGMA index_list(audit_log)"):
                cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
                if cols == [column]:
                    return bool(unique)
        return None

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT doc_id, seq FROM audit_log ORDER BY doc_id, seq"
        ).fetchall() == [("doc-a", 1), ("doc-a", 2), ("doc-b", 1), ("doc-b", 2)]
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0
    assert _unique("doc_id") is False
    assert _unique("seq") is False
    assert len(await get_audit_chain("doc-a")) == 2

    # The explicit operator command is what installs the unified shape.
    migrated = await audit_migrate.migrate_audit_log()
    assert migrated.migrated is True and migrated.legacy_rows == 4
    assert _unique("doc_id") is True
    assert _unique("seq") is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM audit_log_legacy").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 1

    # The legacy bridge keeps the current writer and readers working.
    entry = build_audit_entry("doc-a", "matter-1", "review_completed", "test", {})
    await write_audit_entry(entry)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM audit_log_legacy").fetchone()[0] == 5
    assert len(await get_audit_chain("doc-a")) == 3


async def test_legacy_orm_seq_default_is_zero(tmp_path, monkeypatch):
    """The default-off ORM keeps current main's ``seq=0`` default.

    Legacy rows/manual construction may omit ``seq``; removing the default would
    turn a compatible insert into a NOT NULL failure. Unified writers pass a
    positive seq explicitly and the explicit migration's unique index still
    protects them.
    """
    import sqlite3

    from storage import db
    from storage.audit_log import AuditLogRecord
    from storage.catalog import DocumentRecord
    from storage.db import async_session

    path = tmp_path / "mailroom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()

    async with async_session() as session:
        session.add(
            DocumentRecord(
                doc_id="doc-default",
                matter_id="matter-1",
                original_filename="a.pdf",
                stage="processing",
            )
        )
        await session.flush()  # parent before the FK child
        session.add(
            AuditLogRecord(
                entry_id="entry-default",
                doc_id="doc-default",
                matter_id="matter-1",
                event="classified",
                actor="test",
                detail={},
                prev_hash="",
                entry_hash="hash",
            )
        )
        await session.commit()

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT seq FROM audit_log").fetchall() == [(0,)]
