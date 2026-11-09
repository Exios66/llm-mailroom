"""Roadmap M2: audit tables and the legacy-to-unified migration.

The legacy fixture (``fixtures/audit/legacy_audit.db``) is a database written
by the previous release's writer; ``scripts/build_legacy_audit_fixture.py``
rebuilds it. Postgres is never contacted: its DDL is compiled for the
``postgresql`` dialect and its branch of the migration runs on SQLite.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest
import structlog
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from storage import audit_migrate, db

FIXTURE = Path(__file__).parent / "fixtures" / "audit" / "legacy_audit.db"
LEGACY_COLUMNS = "entry_id, doc_id, matter_id, event, actor, detail, prev_hash, entry_hash, seq, timestamp"
NEW_TABLES = ("audit_log_legacy", "audit_attempts", "audit_addenda", "audit_meta", "audit_trace_io")


@pytest.fixture
def legacy_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "mailroom.db"
    shutil.copy(FIXTURE, path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    return path


def _rows(path: Path, table: str, order: str = "entry_id") -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT {LEGACY_COLUMNS} FROM {table} ORDER BY {order}").fetchall()


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_is_unique(path: Path, table: str, column: str) -> bool | None:
    """True/False for an index on exactly ``column``; None if there is none."""
    with sqlite3.connect(path) as conn:
        for _seq, name, unique, *_ in conn.execute(f"PRAGMA index_list({table})"):
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
            if cols == [column]:
                return bool(unique)
    return None


def _dump(path: Path, tables: tuple[str, ...]) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}


def _process_startup(url: str) -> None:
    """Child-process entry point for the independent-startup regression."""
    os.environ["DATABASE_URL"] = url
    os.environ["MAILROOM_AUDIT_MIGRATE"] = "1"
    from storage import db

    db._schema_checked_url = None
    assert db.ensure_schema()


def test_fixture_is_reproducible(tmp_path, monkeypatch):
    """The committed fixture carries the rows today's writer produces."""
    from scripts import build_legacy_audit_fixture as builder

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///unused")
    monkeypatch.setattr(db, "_schema_checked_url", None)
    out = tmp_path / "rebuilt.db"
    assert builder.build(out) == 11
    assert _rows(out, "audit_log") == _rows(FIXTURE, "audit_log")
    assert _tables(out).isdisjoint(NEW_TABLES)


async def test_migrates_legacy_rows_byte_for_byte(legacy_db):
    before = _rows(legacy_db, "audit_log")
    assert len(before) == 11
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is False

    result = await audit_migrate.migrate_audit_log()

    assert result.migrated is True
    assert result.legacy_rows == 11
    # every legacy column, raw storage included, equals the original
    assert _rows(legacy_db, "audit_log_legacy") == before
    assert _rows(legacy_db, "audit_log") == []
    # the old per-document chains still verify, from the legacy table
    report = await audit_migrate.verify_legacy_chain()
    assert report.ok is True
    assert report.checked == 3
    assert report.failures == []
    # unique doc_id and a global seq on the emptied table
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is True
    assert _index_is_unique(legacy_db, "audit_log", "seq") is True
    # audit_meta records what happened
    import json

    with sqlite3.connect(legacy_db) as conn:
        ((key, value),) = conn.execute("SELECT key, value FROM audit_meta").fetchall()
    meta = json.loads(value)
    assert key == "migration"
    assert meta["legacy_rows"] == 11
    assert len(meta["legacy_tail_hashes_sha256"]) == 64
    assert meta["migrated_at"].endswith("+00:00")


async def test_verify_legacy_chain_flags_a_tampered_legacy_row(legacy_db):
    await audit_migrate.migrate_audit_log()
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("UPDATE audit_log_legacy SET actor='mallory' WHERE entry_id='fixture-doc-b-03'")
    report = await audit_migrate.verify_legacy_chain()
    assert report.ok is False
    assert [f["doc_id"] for f in report.failures] == ["fixture-doc-b"]


async def test_migration_is_idempotent(legacy_db):
    first = await audit_migrate.migrate_audit_log()
    assert first.migrated is True
    snapshot = _dump(legacy_db, ("audit_log", "audit_log_legacy", "audit_meta", "audit_attempts", "audit_addenda"))
    backups = sorted((legacy_db.parent / "backups").iterdir())
    assert len(backups) == 1

    second = await audit_migrate.migrate_audit_log()

    assert second.migrated is False
    assert second.legacy_rows == 11
    assert second.backup_path is None
    assert _dump(legacy_db, tuple(snapshot)) == snapshot
    assert sorted((legacy_db.parent / "backups").iterdir()) == backups


@pytest.mark.parametrize("entry", ["ensure_schema", "migrate_direct"])
async def test_concurrent_startup_migrates_once(legacy_db, entry, monkeypatch):
    before = _rows(legacy_db, "audit_log")
    with structlog.testing.capture_logs() as logs:
        if entry == "ensure_schema":
            # two processes starting together: neither sees the other's cache
            monkeypatch.setenv("MAILROOM_AUDIT_MIGRATE", "1")
            results = await asyncio.gather(
                asyncio.to_thread(db.ensure_schema), asyncio.to_thread(db.ensure_schema)
            )
            assert results == [True, True]
        else:
            # two callers past the cache at the same time: the chain lock decides
            first, second = await asyncio.gather(
                audit_migrate.migrate_audit_log(), audit_migrate.migrate_audit_log()
            )
            assert sorted([first.migrated, second.migrated]) == [False, True]
    assert [e for e in logs if e["event"] == "audit_migration_done"].__len__() == 1
    assert _rows(legacy_db, "audit_log_legacy") == before
    assert _rows(legacy_db, "audit_log") == []
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 1
    assert len(list((legacy_db.parent / "backups").iterdir())) == 1


def test_independent_processes_migrate_once(legacy_db):
    """Two real processes starting together migrate once (not just threads)."""
    ctx = multiprocessing.get_context("spawn")
    url = f"sqlite+aiosqlite:///{legacy_db}"
    processes = [ctx.Process(target=_process_startup, args=(url,)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=90)
        assert process.exitcode == 0

    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log_legacy").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 1
    assert len(list((legacy_db.parent / "backups").iterdir())) == 1


@pytest.mark.parametrize("damage", ["hash", "count"])
async def test_counts_or_hash_mismatch_aborts_without_emptying(legacy_db, monkeypatch, damage):
    before = _rows(legacy_db, "audit_log")
    real_copy = audit_migrate._copy_legacy_rows

    async def damaged_copy(session):
        await real_copy(session)
        from sqlalchemy import text

        if damage == "hash":
            await session.execute(text("UPDATE audit_log_legacy SET entry_hash='x' WHERE entry_id='fixture-doc-a-02'"))
        else:
            await session.execute(text("DELETE FROM audit_log_legacy WHERE entry_id='fixture-doc-a-02'"))

    monkeypatch.setattr(audit_migrate, "_copy_legacy_rows", damaged_copy)

    with pytest.raises(audit_migrate.AuditMigrationError):
        await audit_migrate.migrate_audit_log()

    # nothing moved: the source is intact, the copy and the meta row rolled back
    assert _rows(legacy_db, "audit_log") == before
    assert _rows(legacy_db, "audit_log_legacy") == []
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is False
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0

    # the same data migrates cleanly once the fault is gone
    monkeypatch.setattr(audit_migrate, "_copy_legacy_rows", real_copy)
    result = await audit_migrate.migrate_audit_log()
    assert result.migrated is True
    assert _rows(legacy_db, "audit_log_legacy") == before


async def test_sqlite_backup_written(legacy_db, tmp_path):
    before = _rows(legacy_db, "audit_log")

    default = await audit_migrate.migrate_audit_log()

    # default location: <db dir>/backups/
    backup = Path(default.backup_path)
    assert backup.parent == legacy_db.parent / "backups"
    assert backup.exists()
    # Confidential DB copy: owner-only file and directory permissions.
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    # the backup is the pre-migration database
    assert _rows(backup, "audit_log") == before
    assert _rows(backup, "audit_log_legacy") == []
    assert _index_is_unique(backup, "audit_log", "doc_id") is False
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0

    # an explicit backup_dir is honoured (fresh copy of the fixture)
    other = tmp_path / "other" / "mailroom.db"
    other.parent.mkdir()
    shutil.copy(FIXTURE, other)
    import os

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{other}"  # restored by legacy_db's monkeypatch
    custom_dir = tmp_path / "custom-backups"
    custom = await audit_migrate.migrate_audit_log(backup_dir=custom_dir)
    assert Path(custom.backup_path).parent == custom_dir
    assert _rows(Path(custom.backup_path), "audit_log") == before


async def test_migration_scans_in_bounded_batches(legacy_db, monkeypatch):
    """The copy check and tail digest use keyset batches, not whole-table reads."""
    monkeypatch.setattr(audit_migrate, "BATCH_ROWS", 2)
    result = await audit_migrate.migrate_audit_log()
    assert result.migrated is True
    assert len(_rows(legacy_db, "audit_log_legacy")) == 11
    with sqlite3.connect(legacy_db) as conn:
        ((value,),) = conn.execute("SELECT value FROM audit_meta").fetchall()
    import json

    assert len(json.loads(value)["legacy_tail_hashes_sha256"]) == 64


async def test_explicit_postgres_migration_still_requires_opt_in(legacy_db, monkeypatch):
    """Spec §6: only explicit SQLite bypasses the startup knob, never Postgres."""
    assert db.ensure_schema()
    before = _rows(legacy_db, "audit_log")
    monkeypatch.setattr(audit_migrate, "_dialect_name", lambda session: "postgresql")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)

    result = await audit_migrate.migrate_audit_log()

    assert result.migrated is False
    assert result.reason == "opt_in_required"
    assert result.legacy_rows == 11
    assert _rows(legacy_db, "audit_log") == before
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0


async def test_postgres_table_lock_precedes_deciding_count(monkeypatch):
    """The ACCESS EXCLUSIVE lock must be taken before the authoritative count."""
    import json as _json

    from sqlalchemy.dialects import postgresql

    monkeypatch.setenv("MAILROOM_AUDIT_MIGRATE", "1")
    events: list[tuple[str, str]] = []
    dialect = postgresql.dialect()

    class FakeSession:
        def get_bind(self):
            return type("Bind", (), {"dialect": dialect})()

        async def execute(self, statement, params=None):
            events.append(("execute", str(statement)))

        def add(self, obj):
            events.append(("add", type(obj).__name__))

        async def flush(self):
            events.append(("flush", ""))

    async def no_meta(session):
        return None

    async def fake_scalar(session, sql):
        events.append(("count", sql))
        return 0

    async def not_unified(session):
        return False

    async def fake_write_meta(session, *, legacy_rows, tail_digest):
        events.append(("meta", _json.dumps({"legacy_rows": legacy_rows})))

    monkeypatch.setattr(audit_migrate, "_migration_meta", no_meta)
    monkeypatch.setattr(audit_migrate, "_scalar", fake_scalar)
    monkeypatch.setattr(audit_migrate, "_is_unified_audit_log", not_unified)
    monkeypatch.setattr(audit_migrate, "_write_meta", fake_write_meta)

    result = await audit_migrate._migrate_locked(
        FakeSession(), None, require_opt_in=False
    )

    assert result.reason == "no_legacy_rows"
    assert events[0] == ("execute", "LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE")
    counts = [index for index, event in enumerate(events) if event[0] == "count"]
    assert counts and counts[0] > 0


async def test_startup_requires_opt_in_on_sqlite_and_postgres(legacy_db, monkeypatch):
    before = _dump(legacy_db, ("audit_log",))
    # Tables exist (as they would at startup), so only the opt-in gate decides.
    assert db.ensure_schema()
    # No server is contacted: the dialect is the one seam, the SQL runs on SQLite.
    monkeypatch.setattr(audit_migrate, "_dialect_name", lambda session: "postgresql")

    with structlog.testing.capture_logs() as logs:
        result = await audit_migrate.migrate_at_startup()

    assert result.migrated is False
    assert result.legacy_rows == 11
    assert result.backup_path is None
    assert result.reason == "opt_in_required"
    pending = [e for e in logs if e["event"] == "audit_migration_pending"]
    assert len(pending) == 1
    assert "python -m storage.audit_migrate" in pending[0]["hint"]
    assert _dump(legacy_db, ("audit_log",)) == before
    assert _rows(legacy_db, "audit_log") != []
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is False
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0

    # The explicit CLI path is tested separately on SQLite; this test only
    # covers the startup gate under the forced Postgres dialect seam.


async def test_explicit_migration_needs_no_opt_in(legacy_db):
    """`python -m storage.audit_migrate` is the deliberate path; no env knob."""
    assert db.ensure_schema()
    result = await audit_migrate.migrate_audit_log()
    assert result.migrated is True
    assert result.legacy_rows == 11
    assert len(_rows(legacy_db, "audit_log_legacy")) == 11


async def test_startup_requires_opt_in_on_sqlite(legacy_db):
    """The SQLite startup path is gated exactly like Postgres, by default."""
    before = _dump(legacy_db, ("audit_log",))
    assert db.ensure_schema()
    with structlog.testing.capture_logs() as logs:
        result = await audit_migrate.migrate_at_startup()
    assert result.migrated is False and result.reason == "opt_in_required"
    assert result.legacy_rows == 11
    assert [e for e in logs if e["event"] == "audit_migration_pending"]
    assert _dump(legacy_db, ("audit_log",)) == before
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is False


async def test_startup_fresh_database_is_a_noop_without_opt_in(tmp_path, monkeypatch):
    """A fresh DB keeps the legacy per-event shape until the explicit step."""
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()  # tables, no migration without the knob

    result = await audit_migrate.migrate_at_startup()

    assert result.migrated is False
    assert result.reason == "opt_in_required"
    assert _index_is_unique(path, "audit_log", "doc_id") is False
    assert _index_is_unique(path, "audit_log", "seq") is False
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0

    # The deliberate command installs the marker and unique indexes.
    migrated = await audit_migrate.migrate_audit_log()
    assert migrated.migrated is False and migrated.reason == "no_legacy_rows"
    assert _index_is_unique(path, "audit_log", "doc_id") is True
    assert _index_is_unique(path, "audit_log", "seq") is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 1


def test_ddl_compiles_for_postgres():
    dialect = postgresql.dialect()
    db._ensure_models_imported()
    tables = db.Base.metadata.tables
    for name in ("audit_log", *NEW_TABLES):
        ddl = str(CreateTable(tables[name]).compile(dialect=dialect))
        assert f"CREATE TABLE {name} " in ddl
    # The ORM deliberately leaves audit_log legacy-compatible (no unique
    # doc_id/seq): the explicit migration installs those indexes instead.
    orm_index_ddl = "\n".join(
        str(CreateIndex(ix).compile(dialect=dialect)) for ix in tables["audit_log"].indexes
    )
    assert "CREATE UNIQUE INDEX" not in orm_index_ddl
    # every statement the migration and the rollback run, rendered for Postgres
    statements = audit_migrate.migration_ddl(dialect) + audit_migrate.rollback_ddl(dialect, "20261103T000000Z")
    assert statements
    for statement in statements:
        assert "AUTOINCREMENT" not in statement and "PRAGMA" not in statement
    joined = "\n".join(statements)
    assert "CREATE UNIQUE INDEX ux_audit_log_doc_id ON audit_log (doc_id)" in joined
    assert "CREATE UNIQUE INDEX ux_audit_log_seq ON audit_log (seq)" in joined
    assert "ALTER TABLE audit_log RENAME TO audit_log_v2_20261103T000000Z" in joined
    assert "CREATE TABLE audit_log (" in joined  # the restored legacy shape


async def test_rollback_restores_audit_log(legacy_db):
    before = _rows(legacy_db, "audit_log")
    await audit_migrate.migrate_audit_log()
    # a row the new release wrote after the migration
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO documents (doc_id, matter_id, original_filename, stage, created_at, updated_at) "
            "VALUES ('new-doc', 'matter-9', 'n.pdf', 'archived', '2026-11-03 00:00:00', '2026-11-03 00:00:00')"
        )
        conn.execute(
            "INSERT INTO audit_log (entry_id, doc_id, matter_id, event, actor, detail, prev_hash, entry_hash, seq, timestamp) "
            "VALUES ('new-1', 'new-doc', 'matter-9', 'archived', 'archivist', '{}', '', 'h', 1, '2026-11-03 00:00:00.000000')"
        )

    result = await audit_migrate.rollback_audit_migration()

    assert result.rolled_back is True
    assert result.legacy_rows == 11
    # audit_log holds exactly the legacy rows again, and the previous release's shape
    assert _rows(legacy_db, "audit_log") == before
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is False
    # the new tables are renamed *_v2_<ts>; the row written since stays in them
    tables = _tables(legacy_db)
    renamed = {t for t in tables if "_v2_" in t}
    assert {t.split("_v2_")[0] for t in renamed} == {"audit_log", *NEW_TABLES}
    assert len({t.split("_v2_")[1] for t in renamed}) == 1
    assert set(result.renamed) == {"audit_log", *NEW_TABLES}
    assert tables.isdisjoint(set(NEW_TABLES))  # none left under their live names
    new_log = result.renamed["audit_log"]
    assert [r[0] for r in _rows(legacy_db, new_log)] == ["new-1"]

    # a rollback leaves nothing half done: the release can migrate again
    again = await audit_migrate.migrate_audit_log()
    assert again.migrated is True
    assert again.legacy_rows == 11
    assert _rows(legacy_db, "audit_log_legacy") == before


async def test_rollback_without_migration_is_a_noop(legacy_db):
    before = _rows(legacy_db, "audit_log")
    result = await audit_migrate.rollback_audit_migration()
    assert result.rolled_back is False
    assert _rows(legacy_db, "audit_log") == before


# --- interim bridge: the per-event writer until the row writers land (M3-M8) ---


async def _append_event(doc_id: str = "fixture-doc-a") -> None:
    from schemas.audit import build_audit_entry
    from storage.audit_log import get_latest_audit_hash, write_audit_entry

    entry = build_audit_entry(
        doc_id, "matter-1", "relinked", "test", {"n": 1}, prev_hash=await get_latest_audit_hash(doc_id)
    )
    await write_audit_entry(entry)


async def test_event_writer_appends_to_legacy_table_once_migrated(legacy_db):
    """audit_log holds one row per document after the migration, so the old
    per-event writer appends to audit_log_legacy until the row writers replace it."""
    from storage.audit_log import get_audit_chain

    await audit_migrate.migrate_audit_log()  # explicit, never automatic
    await _append_event()

    assert [e["event"] for e in await get_audit_chain("fixture-doc-a")][-1] == "relinked"
    assert len(await get_audit_chain("fixture-doc-a")) == 5
    assert _rows(legacy_db, "audit_log") == []
    assert len(_rows(legacy_db, "audit_log_legacy")) == 12


async def test_event_writer_stays_on_audit_log_while_migration_is_pending(legacy_db):
    """Default startup (no opt-in): the old audit_log is still the live table."""
    from storage.audit_log import get_audit_chain

    await _append_event()

    assert len(await get_audit_chain("fixture-doc-a")) == 5
    assert len(_rows(legacy_db, "audit_log")) == 12
    assert _rows(legacy_db, "audit_log_legacy") == []


async def test_event_writers_wait_for_migration_before_selecting_model(monkeypatch):
    """The shared Postgres gate precedes document locking and model lookup."""
    import asyncio
    from types import SimpleNamespace
    from storage import audit_log
    from schemas.audit import build_audit_entry

    migration_committed = asyncio.Event()
    both_waiting = asyncio.Event()
    calls = []

    class ModelSelected(Exception):
        pass

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement, params):
            assert str(statement) == "SELECT pg_advisory_xact_lock_shared(hashtext(:key))"
            assert params == {"key": audit_migrate.LOCK_KEY}
            calls.append(("gate", self))
            if len(calls) == 2:
                both_waiting.set()
            await migration_committed.wait()

    async def document_lock(session, key):
        assert migration_committed.is_set()
        assert key.startswith("audit_doc:doc-")
        calls.append(("document", session))

    async def select_model(session):
        assert calls[-1] == ("document", session)
        calls.append(("model", session))
        raise ModelSelected

    monkeypatch.setattr(audit_log, "ensure_schema", lambda: True)
    monkeypatch.setattr(audit_log, "async_session", Session)
    monkeypatch.setattr(audit_log, "acquire_write_lock", document_lock)
    monkeypatch.setattr(audit_log, "event_log_model", select_model)

    async def write(i):
        with pytest.raises(ModelSelected):
            await audit_log.write_audit_entry(
                build_audit_entry(f"doc-{i}", "matter", "event", "test", {})
            )

    tasks = [asyncio.create_task(write(i)) for i in range(2)]
    try:
        await asyncio.wait_for(both_waiting.wait(), timeout=2)
        assert [kind for kind, _ in calls] == ["gate", "gate"]
        migration_committed.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert [kind for kind, _ in calls].count("model") == 2
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_sqlite_event_writer_begins_with_immediate_lock(legacy_db):
    from sqlalchemy import event

    db.ensure_schema()
    statements = []
    engine = db.get_engine().sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        from schemas.audit import build_audit_entry
        from storage.audit_log import write_audit_entry

        await write_audit_entry(
            build_audit_entry("fixture-doc-a", "matter-1", "relinked", "test", {})
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements[0] == "BEGIN IMMEDIATE"
    assert sum(s == "BEGIN IMMEDIATE" for s in statements) == 1
