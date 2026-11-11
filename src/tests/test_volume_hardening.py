"""Volume hardening regressions: bounded watcher concurrency, stale-claim
reconcile safety, and race-free catalog upserts.

Every test here fails on the pre-hardening code (see the commit messages for
the red output) and uses only fake pipelines / temp SQLite files.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import Counter
from pathlib import Path

import pytest


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# H1 — bounded watcher concurrency
# ---------------------------------------------------------------------------


@pytest.fixture
def watcher_env(temp_base_dir, monkeypatch):
    monkeypatch.setenv("WATCHER_POLL_INTERVAL_SECONDS", "0.05")
    monkeypatch.setenv("MAILROOM_WATCHDOG", "0")
    import pipeline.watcher as watcher
    from watchdog.observers.polling import PollingObserver

    # Exercise real filesystem delivery without the platform's native event
    # service (FSEvents startup can block indefinitely in a headless macOS run).
    # Queueing, claiming, rescan and concurrency assertions remain unchanged.
    monkeypatch.setattr(watcher, "Observer", lambda: PollingObserver(timeout=0.05))

    watcher._active_files.clear()
    yield watcher
    watcher._active_files.clear()


def test_watcher_concurrency_is_bounded_and_each_file_runs_once(watcher_env, monkeypatch):
    watcher = watcher_env
    from pipeline import bins

    cap = 3
    monkeypatch.setenv("MAILROOM_MAX_CONCURRENT_DOCS", str(cap))
    inbox = bins.inbox_dir()
    n_files = 40
    for i in range(n_files):
        (inbox / f"doc{i:03d}.txt").write_text(f"document {i}")

    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}
    seen: Counter[str] = Counter()

    def fake_run_pipeline(claimed, matter_id, **kwargs):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            seen[Path(claimed).name] += 1
        time.sleep(0.05)
        with lock:
            state["cur"] -= 1
        return {"doc_id": Path(claimed).stem}

    monkeypatch.setattr(watcher, "run_pipeline", fake_run_pipeline)
    w = watcher.Watcher()
    w.start()
    try:
        assert _wait_for(lambda: sum(seen.values()) >= n_files), dict(seen)
        # let any (buggy) duplicate dispatch surface before asserting
        time.sleep(0.5)
    finally:
        w.stop(drain_timeout=10)

    assert state["peak"] <= cap, f"peak concurrency {state['peak']} exceeded cap {cap}"
    assert len(seen) == n_files
    assert set(seen.values()) == {1}, {k: v for k, v in seen.items() if v != 1}


def test_queued_files_do_not_start_after_stop(watcher_env, monkeypatch):
    watcher = watcher_env
    from pipeline import bins

    monkeypatch.setenv("MAILROOM_MAX_CONCURRENT_DOCS", "1")
    inbox = bins.inbox_dir()
    for i in range(12):
        (inbox / f"q{i:02d}.txt").write_text("x")

    started: list[str] = []
    release = threading.Event()

    def fake_run_pipeline(claimed, matter_id, **kwargs):
        started.append(Path(claimed).name)
        release.wait(timeout=10)
        return {"doc_id": "x"}

    monkeypatch.setattr(watcher, "run_pipeline", fake_run_pipeline)
    w = watcher.Watcher()
    w.start()
    try:
        assert _wait_for(lambda: len(started) == 1)
        time.sleep(0.3)  # the other 11 are queued behind the single slot
        assert len(started) == 1
        stopper = threading.Thread(target=lambda: w.stop(drain_timeout=10))
        stopper.start()
        time.sleep(0.3)
        release.set()  # in-flight doc finishes; queued docs must NOT start
        stopper.join(timeout=15)
        assert not stopper.is_alive()
        time.sleep(0.5)
    finally:
        release.set()
    assert len(started) == 1, started
    # untouched files stay in the inbox for the next watcher run
    assert len(list(bins.inbox_dir().glob("q*.txt"))) == 11


def _fresh_pool(watcher, monkeypatch, cap):
    """An isolated pool wired in as the module's dispatcher."""
    monkeypatch.setenv("MAILROOM_MAX_CONCURRENT_DOCS", str(cap))
    pool = watcher._DispatchPool()
    monkeypatch.setattr(watcher, "_dispatch", pool)
    return pool


def test_admitted_name_is_never_neither_queued_nor_active(watcher_env, monkeypatch):
    """CR4 / T47: a worker used to drop the name from the queue before the
    handler marked it active, so the rescan saw it as idle and queued it
    again. Admission must be one critical section: queued -> active."""
    watcher = watcher_env
    pool = _fresh_pool(watcher, monkeypatch, 1)
    at_entry: dict[str, object] = {}
    entered, go = threading.Event(), threading.Event()

    def handler(path):
        at_entry["in_flight"] = watcher._is_in_flight(path.name)
        at_entry["queued"] = pool.is_queued(path.name)
        entered.set()
        go.wait(timeout=10)
        assert watcher._mark_active(path.name)  # the real handlers' first step
        try:
            pass
        finally:
            watcher._unmark_active(path.name)

    try:
        assert pool.submit("a.txt", handler, Path("a.txt"))
        assert entered.wait(10)
        assert at_entry["in_flight"] is True, "name vanished between dequeue and mark-active"
        assert at_entry["queued"] is False
        # a rescan during that window must not queue a duplicate
        assert not pool.submit("a.txt", handler, Path("a.txt"))
    finally:
        go.set()
        pool.close()
    assert _wait_for(lambda: "a.txt" not in watcher._active_files)


def test_pool_releases_names_for_handlers_that_never_mark_active(watcher_env, monkeypatch):
    watcher = watcher_env
    pool = _fresh_pool(watcher, monkeypatch, 1)
    done = threading.Event()
    try:
        assert pool.submit("plain.txt", lambda p: done.set(), Path("plain.txt"))
        assert done.wait(10)
        assert _wait_for(lambda: not watcher._is_in_flight("plain.txt"))
        # and the name can be submitted again afterwards
        done.clear()
        assert pool.submit("plain.txt", lambda p: done.set(), Path("plain.txt"))
        assert done.wait(10)
    finally:
        pool.close()


def test_admitted_document_is_visible_to_drain_when_close_races_the_start(watcher_env, monkeypatch):
    """T47: close() right after admission must find the document in
    ``_active_files`` (so ``Watcher.stop`` drains it) instead of releasing
    ownership while its handler is about to claim the file."""
    watcher = watcher_env
    pool = _fresh_pool(watcher, monkeypatch, 1)
    entered, go = threading.Event(), threading.Event()

    def handler(path):
        entered.set()
        go.wait(timeout=10)
        watcher._mark_active(path.name)
        watcher._unmark_active(path.name)

    try:
        pool.submit("raced.txt", handler, Path("raced.txt"))
        assert entered.wait(10)
        pool.close()
        assert "raced.txt" in watcher._active_files
    finally:
        go.set()
    assert _wait_for(lambda: "raced.txt" not in watcher._active_files)


def test_admitted_worker_cannot_claim_after_stop_begins(watcher_env, monkeypatch):
    """A worker admitted before shutdown that reaches its claim only after the
    drain timeout must not claim the file: the watcher lock is about to be
    released, and a replacement watcher could claim the same document."""
    watcher = watcher_env
    claims = []
    monkeypatch.setattr(watcher, "claim_file", lambda path, wid: claims.append(path) or path)
    assert watcher._claim_if_open(Path("before.txt"), "w1") == Path("before.txt")
    w = watcher.Watcher()
    w.stop(drain_timeout=0)  # drain times out immediately; lock released
    assert watcher._claim_if_open(Path("late.txt"), "w1") is None
    assert claims == [Path("before.txt")]  # the late file stays in the inbox


@pytest.mark.parametrize("entry_point", ["inbox_event", "startup_scan"])
def test_each_handler_leaves_its_file_unclaimed_when_stop_begins_before_the_claim(
    watcher_env, monkeypatch, entry_point
):
    """Drive the real handler entry points (not ``_claim_if_open``): admit a file,
    hold the handler just before its claim, stop the watcher with no drain time,
    release the handler. A handler that bypassed the gate would claim the file."""
    watcher = watcher_env
    from pipeline import bins

    inbox = bins.inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    doc = inbox / f"{entry_point}.txt"
    doc.write_text("body")
    claims = []
    monkeypatch.setattr(watcher, "claim_file", lambda path, wid: claims.append(path) or path)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)  # the handler's settle delay
    at_claim, go = threading.Event(), threading.Event()

    def held(path):
        at_claim.set()  # the last step before the claim
        assert go.wait(10)
        return False

    monkeypatch.setattr(watcher, "_is_already_processed", held)
    if entry_point == "inbox_event":
        handler = watcher.InboxHandler("w1")._process
    else:
        handler = watcher.Watcher()._process_existing
    worker = threading.Thread(target=handler, args=(doc,), daemon=True)
    worker.start()
    try:
        assert at_claim.wait(10), "handler never reached its claim"
        watcher.Watcher().stop(drain_timeout=0)  # drain gives up at once; claims close
    finally:
        go.set()
        worker.join(10)
    assert not worker.is_alive()
    assert claims == [] and doc.exists()  # still in the inbox for the next watcher


def test_workers_that_outlive_close_still_count_against_the_cap(watcher_env, monkeypatch):
    """T48: after a drain timeout the old workers are still running documents.
    A replacement pool must not add a full set of workers on top of them."""
    watcher = watcher_env
    cap = 2
    pool = _fresh_pool(watcher, monkeypatch, cap)
    release = threading.Event()
    running = []
    lock = threading.Lock()

    def slow(path):
        with lock:
            running.append(path.name)
        release.wait(timeout=20)

    try:
        for i in range(cap):
            pool.submit(f"old{i}.txt", slow, Path(f"old{i}.txt"))
        assert _wait_for(lambda: len(running) == cap)
        pool.close()
        pool.reopen()
        for i in range(cap):
            pool.submit(f"new{i}.txt", slow, Path(f"new{i}.txt"))
        time.sleep(0.4)
        assert sorted(running) == ["old0.txt", "old1.txt"], running  # new work waits for a free slot
        release.set()
        # once the survivors finish, the waiting documents run (no stall)
        assert _wait_for(lambda: len(running) == 2 * cap), running
    finally:
        release.set()
        pool.close()


def test_max_concurrent_docs_config_and_env_override(monkeypatch):
    from pipeline.config import get_max_concurrent_docs

    monkeypatch.delenv("MAILROOM_MAX_CONCURRENT_DOCS", raising=False)
    assert get_max_concurrent_docs() == 4  # taxonomy.yaml watcher.max_concurrent_docs
    monkeypatch.setenv("MAILROOM_MAX_CONCURRENT_DOCS", "7")
    assert get_max_concurrent_docs() == 7
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv("MAILROOM_MAX_CONCURRENT_DOCS", bad)
        assert get_max_concurrent_docs() == 4


# ---------------------------------------------------------------------------
# H2 — stale-claim reconcile must not hijack in-flight documents
# ---------------------------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_claim_stamps_claim_time_not_upload_time(temp_base_dir):
    from pipeline import bins

    upload = bins.inbox_dir() / "backlogged.txt"
    upload.write_text("hello")
    _age(upload, 2 * 3600)  # waited two hours in the inbox backlog

    claimed = bins.claim_file(upload, "worker-A")

    assert time.time() - claimed.stat().st_mtime < 60
    assert bins.list_stale_processing_files(60) == []


def test_requeue_from_review_stamps_claim_time(temp_base_dir):
    from pipeline import bins

    parked = bins.review_dir() / "parked.txt"
    parked.write_text("hello")
    _age(parked, 5 * 3600)

    resumed = bins.requeue_from_review(parked, "worker-A")

    assert time.time() - resumed.stat().st_mtime < 60
    assert bins.list_stale_processing_files(60) == []


def test_reconcile_leaves_active_document_in_processing(temp_base_dir):
    import pipeline.watcher as watcher
    from pipeline import bins

    upload = bins.inbox_dir() / "backlogged.txt"
    upload.write_text("hello")
    _age(upload, 2 * 3600)
    claimed = bins.claim_file(upload, "worker-A")
    # Even if the claim looks old (clock skew, utime unsupported), a document
    # this process is running must not be moved back to the inbox.
    _age(claimed, 2 * 3600)

    assert watcher._mark_active("backlogged.txt")
    try:
        watcher.Watcher()._reconcile_stale_claims()
    finally:
        watcher._unmark_active("backlogged.txt")

    assert claimed.exists()
    assert not (bins.inbox_dir() / "backlogged.txt").exists()


def test_reconcile_still_requeues_orphans_of_a_dead_worker(temp_base_dir):
    import pipeline.watcher as watcher
    from pipeline import bins

    orphan_dir = bins.processing_dir("dead-worker")
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "orphan.txt"
    orphan.write_text("stranded")
    _age(orphan, 2 * 3600)

    watcher.Watcher()._reconcile_stale_claims()

    assert not orphan.exists()
    assert (bins.inbox_dir() / "orphan.txt").exists()


# ---------------------------------------------------------------------------
# H3 — catalog writes are race-free upserts
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_db(tmp_path, monkeypatch):
    from storage import db

    path = tmp_path / "catalog.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(db, "_schema_checked_url", None)
    assert db.ensure_schema()
    return path


def _rows(path, sql):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_same_doc_written_by_8_threads_is_one_row_with_no_errors(catalog_db):
    from schemas.matter import Matter
    from storage.catalog import write_document_record, write_matter_record

    n_threads, n_docs = 8, 25
    barrier = threading.Barrier(n_threads)
    errors: list[str] = []

    def worker():
        async def run():
            for i in range(n_docs):
                barrier.wait(timeout=60)  # all threads hit the same doc_id together
                doc_id = f"DOC-{i:03d}"
                try:
                    await write_matter_record(Matter(matter_id=f"M-{i % 5}", name=f"matter {i % 5}", client_name=""))
                    for stage in ("processing", "classified", "archived"):
                        await write_document_record(
                            {
                                "doc_id": doc_id,
                                "matter_id": f"M-{i % 5}",
                                "original_filename": f"{doc_id}.txt",
                                "stage": stage,
                            }
                        )
                except Exception as exc:  # noqa: BLE001 - the test asserts there are none
                    errors.append(f"{type(exc).__name__}: {str(exc)[:90]}")

        asyncio.run(run())

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    [t.start() for t in threads]
    [t.join(timeout=300) for t in threads]

    assert errors == []
    rows = _rows(catalog_db, "SELECT doc_id, stage FROM documents")
    assert len(rows) == n_docs
    assert {stage for _doc, stage in rows} == {"archived"}
    assert len({doc for doc, _stage in rows}) == n_docs
    assert len(_rows(catalog_db, "SELECT matter_id FROM matters")) == 5


def test_32_threads_mixed_catalog_ops_lose_nothing(catalog_db):
    from schemas.matter import Matter
    from storage.catalog import (
        update_document_scores,
        write_document_record,
        write_matter_record,
    )

    n_threads, docs_each = 32, 3
    errors: list[str] = []

    def worker(tid):
        async def run():
            for i in range(docs_each):
                doc_id = f"T{tid:02d}-{i}"
                try:
                    await write_matter_record(Matter(matter_id="SHARED", name="shared", client_name=""))
                    await write_document_record(
                        {
                            "doc_id": doc_id,
                            "matter_id": "SHARED",
                            "original_filename": f"{doc_id}.txt",
                            "stage": "processing",
                        }
                    )
                    await update_document_scores(doc_id, {"success_rate": 1.0})
                    await write_document_record(
                        {
                            "doc_id": doc_id,
                            "matter_id": "SHARED",
                            "original_filename": f"{doc_id}.txt",
                            "stage": "archived",
                            "doc_type": "contract",
                            "extracted_data": {"k": "v" * 2000},
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{type(exc).__name__}: {str(exc)[:90]}")

        asyncio.run(run())

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    [t.start() for t in threads]
    [t.join(timeout=300) for t in threads]

    assert errors == []
    rows = _rows(catalog_db, "SELECT doc_id, stage, doc_type, scores FROM documents")
    assert len(rows) == n_threads * docs_each
    assert {(stage, dtype) for _d, stage, dtype, _s in rows} == {("archived", "contract")}
    # the archive upsert names no `scores` key, so the score written between
    # the two upserts must survive it on every row
    import json

    assert [doc for doc, _st, _ty, scores in rows if json.loads(scores or "null") != {"success_rate": 1.0}] == []
    assert len(_rows(catalog_db, "SELECT matter_id FROM matters")) == 1


def test_upsert_keeps_unspecified_columns_and_returns_the_row(catalog_db):
    """Behavior parity with the old get-then-write: absent keys are left
    alone, explicit keys overwrite, matter name/client fall back when blank."""
    from schemas.matter import Matter
    from storage.catalog import (
        get_document,
        write_document_record,
        write_matter_record,
    )

    async def run():
        first = await write_matter_record(
            Matter(matter_id="M1", name="Acme v. Beta", client_name="Acme")
        )
        assert (first.matter_id, first.name, first.client_name) == ("M1", "Acme v. Beta", "Acme")
        again = await write_matter_record(Matter(matter_id="M1", name="", client_name=""))
        assert (again.name, again.client_name) == ("Acme v. Beta", "Acme")

        rec = await write_document_record(
            {
                "doc_id": "D1",
                "matter_id": "M1",
                "original_filename": "a.txt",
                "stage": "processing",
                "doc_type": "contract",
                "classification_confidence": 0.9,
                "extracted_data": {"a": 1},
            }
        )
        assert rec.doc_id == "D1" and rec.stage == "processing"
        rec = await write_document_record(
            {"doc_id": "D1", "matter_id": "M1", "original_filename": "ignored.txt", "stage": "archived"}
        )
        assert rec.stage == "archived"
        got = await get_document("D1")
        assert got.stage == "archived"
        assert got.doc_type == "contract"  # not in the second write: kept
        assert got.classification_confidence == 0.9
        assert got.extracted_data == {"a": 1}
        assert got.original_filename == "a.txt"  # never rewritten on update
        assert got.updated_at >= got.created_at

    asyncio.run(run())


def test_first_insert_stamps_created_and_updated_with_one_timestamp(catalog_db):
    """T55: a new row's created_at and updated_at come from the same instant;
    an update moves updated_at only."""
    from storage.catalog import get_document, write_document_record

    async def run():
        first = {"doc_id": "TS1", "matter_id": "M", "original_filename": "t.txt", "stage": "inbox"}
        await write_document_record(first)
        row = await get_document("TS1")
        assert row.created_at == row.updated_at
        created = row.created_at
        await write_document_record({**first, "stage": "archived"})
        row = await get_document("TS1")
        assert row.created_at == created and row.updated_at > created

    asyncio.run(run())


def test_write_retries_on_database_is_locked(catalog_db, monkeypatch):
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from storage import db

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(db.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))
        return "ok"

    assert asyncio.run(db.run_write_with_retry(flaky)) == "ok"
    assert calls["n"] == 3 and len(sleeps) == 2 and all(0 < s <= 2.0 for s in sleeps)

    calls["n"] = 0
    sleeps.clear()

    async def broken():
        calls["n"] += 1
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("no such table: x"))

    with pytest.raises(OperationalError):
        asyncio.run(db.run_write_with_retry(broken))  # not a lock error: no retry
    assert calls["n"] == 1 and sleeps == []

    calls["n"] = 0
    sleeps.clear()

    async def always_locked():
        calls["n"] += 1
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))

    with pytest.raises(OperationalError):
        asyncio.run(db.run_write_with_retry(always_locked, attempts=3))
    # every attempt ran, with a backoff between consecutive attempts only
    assert calls["n"] == 3 and len(sleeps) == 2


@pytest.mark.parametrize("attempts", [0, -1])
def test_write_retry_rejects_a_non_positive_attempt_count(attempts):
    """T74: ``attempts < 1`` skipped the loop and returned None, i.e. the write
    was silently never attempted. It must fail loudly without running ``op``."""
    from storage import db

    ran = {"n": 0}

    async def op():
        ran["n"] += 1
        return "written"

    with pytest.raises(ValueError, match="attempts"):
        asyncio.run(db.run_write_with_retry(op, attempts=attempts))
    assert ran["n"] == 0


def test_sqlite_connections_set_busy_timeout_before_wal(catalog_db, monkeypatch):
    """busy_timeout must be armed before any other pragma: the WAL switch
    itself takes a lock and used to fail instantly under load."""
    import sqlite3

    from storage import db

    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "5000")  # the env may set any value
    statements: list[str] = []

    class Spy:
        def __init__(self, real):
            self.real = real

        def cursor(self):
            return self

        def execute(self, sql, *a):
            statements.append(sql)
            return self.real.execute(sql, *a)

        def close(self):
            pass

    conn = sqlite3.connect(catalog_db)
    try:
        db._apply_sqlite_pragmas(Spy(conn))
        assert statements[0].lower().startswith("pragma busy_timeout")
        assert any("journal_mode=wal" in s.lower().replace(" ", "") for s in statements)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        conn.close()


async def test_audit_write_retries_sqlite_lock(monkeypatch):
    """A `database is locked` on BEGIN IMMEDIATE is retried, not dropped."""
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from storage import audit_log

    calls = []

    async def flaky(entry):
        calls.append(entry)
        if len(calls) == 1:
            raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))
        return "ok"

    monkeypatch.setattr(audit_log, "_write_audit_entry_once", flaky)
    assert await audit_log.write_audit_entry("entry") == "ok"
    assert len(calls) == 2


def test_run_coro_in_a_running_loop_waits_without_a_false_timeout(monkeypatch):
    """CR7: the helper-thread branch must not time out a write that is still running.

    The ``with`` block waits for the thread regardless, so a ``result(timeout=...)``
    only turned a slow-but-committed write into a logged TimeoutError.
    """
    import asyncio
    import concurrent.futures

    from graph.build_graph import _run_coro

    seen = []
    real_result = concurrent.futures.Future.result

    def recording_result(self, timeout=None):
        seen.append(timeout)
        return real_result(self, timeout)

    monkeypatch.setattr(concurrent.futures.Future, "result", recording_result)

    async def write():
        await asyncio.sleep(0.05)
        return "committed"

    async def caller():
        return _run_coro(write)

    assert asyncio.run(caller()) == "committed"
    assert seen and all(t is None for t in seen)


# ---------------------------------------------------------------------------
# #203 review: a running document must not look stale to another process
# ---------------------------------------------------------------------------


def test_node_heartbeat_refreshes_the_claim_time_of_a_processing_file(temp_base_dir):
    """The stale-claim sweep may run in a dedicated watcher process that cannot see
    this process's in-flight set; a claim a run keeps advancing must not age out."""
    from graph.build_graph import _touch_heartbeat
    from pipeline import bins

    claim_dir = bins.processing_dir("w-live")
    claim_dir.mkdir(parents=True, exist_ok=True)
    claimed = claim_dir / "long.txt"
    claimed.write_text("body")
    hours_ago = time.time() - 2 * 3600
    os.utime(claimed, (hours_ago, hours_ago))
    assert claimed in bins.list_stale_processing_files(stale_minutes=60)

    _touch_heartbeat({"file_path": str(claimed)})

    assert claimed not in bins.list_stale_processing_files(stale_minutes=60)


def test_node_heartbeat_leaves_files_outside_processing_alone(temp_base_dir):
    from graph.build_graph import _touch_heartbeat
    from pipeline import bins

    parked = bins.review_dir() / "parked.txt"
    parked.write_text("body")
    hours_ago = time.time() - 2 * 3600
    os.utime(parked, (hours_ago, hours_ago))
    _touch_heartbeat({"file_path": str(parked)})
    assert abs(parked.stat().st_mtime - hours_ago) < 1


# ---------------------------------------------------------------------------
# #203 review: a delayed writer cannot move updated_at backwards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("second_write", ["upsert", "update_only"])
def test_a_delayed_writer_does_not_move_updated_at_backwards(catalog_db, monkeypatch, second_write):
    """Each writer reads its clock before its SQL runs, so a slow one can arrive
    carrying an earlier instant than the row already holds."""
    import datetime as dt

    from storage import catalog

    class Clock(dt.datetime):
        now_value = None

        @classmethod
        def now(cls, tz=None):
            return cls.now_value

    monkeypatch.setattr(catalog, "datetime", Clock)
    late = dt.datetime(2026, 1, 1, 12, 0, 5, tzinfo=dt.timezone.utc)
    early = dt.datetime(2026, 1, 1, 12, 0, 1, tzinfo=dt.timezone.utc)
    base = {"doc_id": "MONO", "matter_id": "M", "original_filename": "m.txt", "stage": "inbox"}

    async def run():
        Clock.now_value = late
        await catalog.write_document_record(base)  # the writer that captured the later instant
        Clock.now_value = early  # the delayed writer, still holding its earlier instant
        if second_write == "upsert":
            await catalog.write_document_record({**base, "stage": "archived"})
        else:
            await catalog.write_document_record({"doc_id": "MONO", "stage": "archived"})
        row = await catalog.get_document("MONO")
        assert row.stage == "archived"  # the stage update still lands
        assert row.updated_at.replace(tzinfo=None) == late.replace(tzinfo=None)
        assert row.updated_at >= row.created_at

    asyncio.run(run())
