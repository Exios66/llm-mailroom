"""Real SQLite regressions for first-use threads and short-lived event loops."""

import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time
import weakref

import pytest
from sqlalchemy import event
from sqlalchemy.pool import NullPool

from storage import db


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(db, "_schema_checked_url", None)
    return path


def test_first_schema_creation_is_serialized_across_threads(database):
    # Widen SQLite's real checkfirst/create window, without replacing its DDL.
    db._ensure_models_imported()
    def pause_before_create(*args, **kwargs):
        time.sleep(0.05)
    event.listen(db.Base.metadata, "before_create", pause_before_create)
    start = threading.Barrier(6)
    def create():
        start.wait(timeout=5)
        return db.ensure_schema()
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: create(), range(6)))
    finally:
        event.remove(db.Base.metadata, "before_create", pause_before_create)
    assert results == [True] * 6
    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"documents", "audit_log", "relation_log", "relation_edges"} <= tables


def test_concurrent_event_loops_have_their_own_engine_and_session(database):
    start = threading.Barrier(2)
    async def inspect_engine():
        engine = db.get_engine()
        start.wait(timeout=5)
        session = db.async_session()
        try:
            assert session.bind is engine
            assert isinstance(engine.pool, NullPool)
            return engine
        finally:
            await session.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        engines = list(executor.map(lambda _: asyncio.run(inspect_engine()), range(2)))
    assert engines[0] is not engines[1]


def test_closed_loops_never_reuse_engine_even_with_colliding_integer_ids(database, monkeypatch):
    # Any future id(loop)-keyed cache would collide on every iteration here.
    monkeypatch.setattr(db, "id", lambda _: 7, raising=False)
    engines = []
    loops = []
    async def inspect_engine():
        engine = db.get_engine()
        assert db.get_engine() is engine
        return engine
    for _ in range(8):
        loop = asyncio.new_event_loop()
        loops.append(loop)  # retain closed loops to exercise explicit pruning
        try:
            engines.append(loop.run_until_complete(inspect_engine()))
        finally:
            loop.close()
    assert len({id(engine) for engine in engines}) == 8


def test_cache_uses_weak_loop_references(database):
    async def inspect_engine():
        db.get_engine()
    loop = asyncio.new_event_loop()
    reference = weakref.ref(loop)
    loop.run_until_complete(inspect_engine())
    loop.close()
    del loop
    gc.collect()
    assert reference() is None


def test_closed_loop_entries_are_pruned_even_if_loop_is_retained(database):
    async def inspect_engine():
        return db.get_engine()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(inspect_engine())
    loop.close()
    async def inspect_cache():
        db.get_engine()
        assert loop not in db._loop_states
        assert all(not owner.is_closed() for owner in db._loop_states)
    asyncio.run(inspect_cache())


def test_database_url_change_rebinds_session_on_same_loop(database, monkeypatch, tmp_path):
    async def change_url():
        first = db.get_engine()
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'second.db'}")
        second = db.get_engine()
        assert second is not first
        async with db.async_session() as session:
            assert session.bind is second
        assert db.get_engine() is second
    asyncio.run(change_url())


def test_first_use_concurrent_audit_and_relations_writes_are_all_persisted(database):
    from schemas.audit import build_audit_entry
    from storage.audit_log import write_audit_entry
    from storage.relations import write_relation_log_entry
    start = threading.Barrier(4)
    async def write(index):
        entry = build_audit_entry("shared-document", "matter", "concurrent", "test", {"index": index})
        await write_audit_entry(entry)
        result = await write_relation_log_entry("concurrent", {"index": index})
        assert result["entry_id"]
    async def bounded_write(index):
        await asyncio.wait_for(write(index), timeout=10)
    def worker(index):
        start.wait(timeout=5)
        asyncio.run(bounded_write(index))
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(worker, range(4)))
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 4
        assert conn.execute("SELECT seq FROM audit_log ORDER BY seq").fetchall() == [(1,), (2,), (3,), (4,)]
        assert conn.execute("SELECT count(*) FROM relation_log").fetchone()[0] == 4
        for table in ("audit_log", "relation_log"):
            rows = conn.execute(f"SELECT prev_hash, entry_hash FROM {table}").fetchall()
            assert sum(not prev for prev, _ in rows) == 1
            hashes = {entry_hash for _, entry_hash in rows}
            assert len(hashes) == 4
            assert all(not prev or prev in hashes for prev, _ in rows)
            assert len({prev for prev, _ in rows}) == 4


def test_all_async_backends_avoid_persistent_loop_bound_pools():
    # asyncpg pools can retain their event loop just as aiosqlite pools can.
    # Verify the actual SQLAlchemy engine factory receives the same policy.
    for url in ("sqlite+aiosqlite:///:memory:",
                "postgresql+asyncpg://user:password@localhost/mailroom"):
        assert db._engine_kwargs(url)["poolclass"] is NullPool
