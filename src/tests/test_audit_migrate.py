"""Roadmap M2: audit tables and the legacy-to-unified migration.

The legacy fixture (``fixtures/audit/legacy_audit.db``) is a database written
by the previous release's writer; ``scripts/build_legacy_audit_fixture.py``
rebuilds it. Postgres is never contacted: its DDL is compiled for the
``postgresql`` dialect and its branch of the migration runs on SQLite.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
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
async def test_concurrent_startup_migrates_once(legacy_db, entry):
    before = _rows(legacy_db, "audit_log")
    with structlog.testing.capture_logs() as logs:
        if entry == "ensure_schema":
            # two processes starting together: neither sees the other's cache
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


async def test_postgres_requires_opt_in(legacy_db, monkeypatch):
    before = _dump(legacy_db, ("audit_log",))
    # No Postgres in tests: the dialect is the one seam, the SQL runs on SQLite.
    monkeypatch.setattr(audit_migrate, "_dialect_name", lambda session: "postgresql")

    with structlog.testing.capture_logs() as logs:
        result = await audit_migrate.migrate_audit_log()

    assert result.migrated is False
    assert result.legacy_rows == 11
    assert result.backup_path is None
    pending = [e for e in logs if e["event"] == "audit_migration_pending"]
    assert len(pending) == 1
    assert "restart with MAILROOM_AUDIT_MIGRATE=1" in pending[0]["hint"]
    assert "`MAILROOM_AUDIT_MIGRATE=1 python -m storage.audit_migrate`" in pending[0]["hint"]
    assert _dump(legacy_db, ("audit_log",)) == before
    assert _rows(legacy_db, "audit_log") != []
    assert _index_is_unique(legacy_db, "audit_log", "doc_id") is False
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 0

    # with the opt-in it migrates, in one transaction and without a file backup
    monkeypatch.setenv("MAILROOM_AUDIT_MIGRATE", "1")
    opted = await audit_migrate.migrate_audit_log()
    assert opted.migrated is True
    assert opted.backup_path is None
    assert len(_rows(legacy_db, "audit_log_legacy")) == 11


async def test_postgres_fresh_database_needs_no_opt_in(tmp_path, monkeypatch):
    """Nothing to move, nothing to lose: an empty audit_log is not gated."""
    path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.delenv("MAILROOM_AUDIT_MIGRATE", raising=False)
    monkeypatch.setattr(db, "_schema_checked_url", None)
    monkeypatch.setattr(audit_migrate, "_dialect_name", lambda session: "postgresql")

    result = await audit_migrate.migrate_audit_log()

    assert result.migrated is False
    assert result.legacy_rows == 0
    assert _index_is_unique(path, "audit_log", "doc_id") is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM audit_meta").fetchone()[0] == 1


def test_ddl_compiles_for_postgres():
    dialect = postgresql.dialect()
    db._ensure_models_imported()
    tables = db.Base.metadata.tables
    for name in ("audit_log", *NEW_TABLES):
        ddl = str(CreateTable(tables[name]).compile(dialect=dialect))
        assert f"CREATE TABLE {name} " in ddl
    # indexes the models declare, including the two unique ones on audit_log
    index_ddl = "\n".join(str(CreateIndex(ix).compile(dialect=dialect)) for ix in tables["audit_log"].indexes)
    assert "CREATE UNIQUE INDEX ix_audit_log_doc_id ON audit_log (doc_id)" in index_ddl
    assert "CREATE UNIQUE INDEX ix_audit_log_seq ON audit_log (seq)" in index_ddl
    # every statement the migration and the rollback run, rendered for Postgres
    statements = audit_migrate.migration_ddl(dialect) + audit_migrate.rollback_ddl(dialect, "20261103T000000Z")
    assert statements
    for statement in statements:
        assert "AUTOINCREMENT" not in statement and "PRAGMA" not in statement
    joined = "\n".join(statements)
    assert "CREATE UNIQUE INDEX ix_audit_log_doc_id ON audit_log (doc_id)" in joined
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

    await _append_event()  # ensure_schema() migrates first

    assert [e["event"] for e in await get_audit_chain("fixture-doc-a")][-1] == "relinked"
    assert len(await get_audit_chain("fixture-doc-a")) == 5
    assert _rows(legacy_db, "audit_log") == []
    assert len(_rows(legacy_db, "audit_log_legacy")) == 12


async def test_event_writer_stays_on_audit_log_while_migration_is_pending(legacy_db, monkeypatch):
    """Postgres without the opt-in: the old audit_log is still the live table."""
    from storage.audit_log import get_audit_chain

    monkeypatch.setattr(audit_migrate, "_dialect_name", lambda session: "postgresql")

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
        assert key.startswith("audit_log:doc-")
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
