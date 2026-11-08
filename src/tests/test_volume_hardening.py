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
    rows = _rows(catalog_db, "SELECT doc_id, stage, doc_type FROM documents")
    assert len(rows) == n_threads * docs_each
    assert {(stage, dtype) for _d, stage, dtype in rows} == {("archived", "contract")}
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

    async def broken():
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("no such table: x"))

    with pytest.raises(OperationalError):
        asyncio.run(db.run_write_with_retry(broken))  # not a lock error: no retry

    async def always_locked():
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))

    with pytest.raises(OperationalError):
        asyncio.run(db.run_write_with_retry(always_locked, attempts=3))


def test_sqlite_connections_set_busy_timeout_before_wal(catalog_db):
    """busy_timeout must be armed before any other pragma: the WAL switch
    itself takes a lock and used to fail instantly under load."""
    import sqlite3

    from storage import db

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
