"""#219 (follow-up to #203): the watcher tracks in-flight documents by claimed path.

The in-flight set was keyed by the inbox file name, but reconcile compares paths in
``processing/<worker>/``. So an orphaned claim of a dead worker was skipped as "in flight"
whenever a different document of the same name was running, and an orphan of the same name
in this watcher's own worker directory made every new claim of that name fail. A claim is now
identified by its path: reconcile only spares paths a running document holds, and a new claim
that finds its name taken by an orphan reconciles the orphan instead of failing.

Hermetic: ``temp_base_dir`` is a per-test directory, the handlers are called directly (no pool
or observer), and ``run_pipeline`` is a fake. The stale-claim race uses two synchronized threads.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def env(temp_base_dir, monkeypatch):
    import pipeline.watcher as watcher

    monkeypatch.setattr(watcher, "_claims_open", True)
    monkeypatch.setattr(watcher, "_active_files", set())
    monkeypatch.setattr(watcher, "_active_claims", set(), raising=False)
    return watcher


def _age(path: Path, seconds: float = 2 * 3600) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _fake_pipeline(calls):
    def run(claimed, matter_id, **kwargs):
        calls.append((Path(claimed), Path(claimed).read_text()))
        return {"doc_id": Path(claimed).stem}

    return run


def test_dead_workers_orphan_is_reconciled_while_a_same_name_document_runs(env):
    """Was skipped: the running document's inbox name hid the orphan from reconcile."""
    watcher = env
    from pipeline import bins

    live_src = bins.inbox_dir() / "a.txt"
    live_src.write_text("the document that is running")
    w = watcher.Watcher()
    assert watcher._mark_active("a.txt")  # what the handler does before it claims
    live = watcher._claim_if_open(live_src, w.worker_id)
    assert live is not None
    try:
        orphan_dir = bins.processing_dir("dead-worker")
        orphan_dir.mkdir(parents=True, exist_ok=True)
        orphan = orphan_dir / "a.txt"
        orphan.write_text("the orphan of a dead worker")
        _age(orphan)

        w._reconcile_stale_claims()

        assert not orphan.exists(), "the orphan was left stranded behind a same-name document"
        assert (bins.inbox_dir() / "a.txt").read_text() == "the orphan of a dead worker"
        assert live.read_text() == "the document that is running"
    finally:
        watcher._release_claim(live)
        watcher._unmark_active("a.txt")


def test_reconcile_never_touches_the_claim_a_document_is_running(env):
    watcher = env
    from pipeline import bins

    src = bins.inbox_dir() / "b.txt"
    src.write_text("running")
    w = watcher.Watcher()
    assert watcher._mark_active("b.txt")
    claimed = watcher._claim_if_open(src, w.worker_id)
    _age(claimed)  # clock skew / no utime support: it merely looks stale
    try:
        w._reconcile_stale_claims()
        assert claimed.exists()
        assert not (bins.inbox_dir() / "b.txt").exists()
    finally:
        watcher._release_claim(claimed)
        watcher._unmark_active("b.txt")
    w._reconcile_stale_claims()  # once released it is an ordinary stale claim
    assert not claimed.exists()


def test_stale_sweep_cannot_move_a_new_same_path_claim(env, monkeypatch):
    """A stale check and its move must be atomic with a replacement claim."""
    watcher = env
    from pipeline import bins

    w = watcher.Watcher()
    orphan_dir = bins.processing_dir(w.worker_id)
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "race.txt"
    orphan.write_text("old orphan")
    _age(orphan)
    fresh = bins.inbox_dir() / "race.txt"
    fresh.write_text("new document")

    checked = threading.Event()
    release_sweep = threading.Event()
    claim_started = threading.Event()
    gate_checked = threading.Event()
    claim_finished = threading.Event()
    gate_state: list[str] = []
    claimed: list[Path | None] = []
    errors: list[BaseException] = []
    original = watcher._is_claim_in_flight

    class ProbeGate:
        """Expose whether the claimant acquired the real gate or had to wait."""

        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            if threading.current_thread().name == "new-claim":
                acquired = self.lock.acquire(blocking=False)
                gate_state.append("acquired" if acquired else "contended")
                gate_checked.set()
                if not acquired:
                    self.lock.acquire()
            else:
                self.lock.acquire()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.lock.release()

    def pause_after_check(path):
        answer = original(path)
        if threading.current_thread().name == "stale-sweep" and path == orphan:
            checked.set()
            if not release_sweep.wait(5):
                raise AssertionError("sweep was not released")
        return answer

    def sweep():
        try:
            w._reconcile_stale_claims()
        except BaseException as exc:
            errors.append(exc)

    def claim():
        try:
            assert watcher._mark_active(fresh.name)
            claim_started.set()
            claimed.append(watcher._claim_if_open(fresh, w.worker_id))
        except BaseException as exc:
            errors.append(exc)
        finally:
            claim_finished.set()

    with monkeypatch.context() as patcher:
        patcher.setattr(watcher, "_claim_gate", ProbeGate(watcher._claim_gate))
        patcher.setattr(watcher, "_is_claim_in_flight", pause_after_check)
        sweeper = threading.Thread(target=sweep, name="stale-sweep")
        claimant = threading.Thread(target=claim, name="new-claim")
        sweeper.start()
        try:
            assert checked.wait(5)
            claimant.start()
            assert claim_started.wait(5)
            assert gate_checked.wait(5)
            if gate_state == ["acquired"]:
                # Old code leaves the sweep outside the gate. Finish the real
                # replacement claim before the paused sweep resumes.
                assert claim_finished.wait(5)
            else:
                assert gate_state == ["contended"]
        finally:
            release_sweep.set()
            sweeper.join(5)
            if claimant.ident is not None:
                claimant.join(5)
            watcher._unmark_active(fresh.name)
            if claimed:
                watcher._release_claim(claimed[0])

    assert not sweeper.is_alive() and not claimant.is_alive()
    assert not errors
    assert claimed and claimed[0] is not None
    assert claimed[0].exists(), "stale sweep moved a newly registered live claim"
    assert claimed[0].read_text() == "new document"
    assert gate_state == ["contended"]


@pytest.mark.parametrize("same_bytes", [True, False], ids=["same-bytes", "different-bytes"])
def test_new_claim_over_an_orphan_of_the_same_name_runs_once(env, monkeypatch, same_bytes):
    """Was skipped: the orphan occupied ``processing/<worker>/a.txt`` so every claim raised."""
    watcher = env
    from pipeline import bins

    w = watcher.Watcher()
    orphan_dir = bins.processing_dir(w.worker_id)
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / "c.txt"
    orphan.write_text("new document" if same_bytes else "old orphan")
    _age(orphan)
    new = bins.inbox_dir() / "c.txt"
    new.write_text("new document")

    calls: list = []
    monkeypatch.setattr(watcher, "run_pipeline", _fake_pipeline(calls))
    w._process_existing(new)

    assert calls == [(orphan, "new document")], calls  # run once, on the new bytes
    assert not (bins.inbox_dir() / "c.txt").exists()
    stale_copy = bins.inbox_dir() / "c--stale.txt"
    if same_bytes:
        assert not stale_copy.exists()  # the identical orphan was dropped, not queued twice
    else:
        assert stale_copy.read_text() == "old orphan"  # a different document is kept, for its own run
    assert not watcher._is_claim_in_flight(orphan)  # released when the run ended


def test_new_claim_is_not_double_run_while_a_live_claim_holds_the_name(env, monkeypatch):
    watcher = env
    from pipeline import bins

    w = watcher.Watcher()
    first_src = bins.inbox_dir() / "d.txt"
    first_src.write_text("first")
    live = watcher._claim_if_open(first_src, w.worker_id)
    assert live is not None
    try:
        second = bins.inbox_dir() / "d.txt"
        second.write_text("second")
        calls: list = []
        monkeypatch.setattr(watcher, "run_pipeline", _fake_pipeline(calls))
        w._process_existing(second)
        assert calls == []  # not run on top of the live claim
        assert second.read_text() == "second"  # and left in the inbox for the next pass
        assert live.read_text() == "first"
    finally:
        watcher._release_claim(live)


def test_inbox_handler_releases_its_claim_when_the_run_fails(env, monkeypatch):
    watcher = env
    from pipeline import bins

    def boom(claimed, matter_id, **kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(watcher, "run_pipeline", boom)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(watcher, "_finalize_claimed_on_error", lambda *a, **k: None)
    src = bins.inbox_dir() / "e.txt"
    src.write_text("x")
    handler = watcher.InboxHandler("handler-worker")
    handler._process(src)
    assert watcher._active_claims == set()
    assert not watcher._is_in_flight("e.txt")


def test_claim_tolerates_an_orphan_the_periodic_reconcile_already_moved(env, monkeypatch):
    """Reconcile raced the claim and moved the orphan first: the name is free, the claim goes on."""
    watcher = env
    from pipeline import bins

    w = watcher.Watcher()
    orphan_dir = bins.processing_dir(w.worker_id)
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "f.txt").write_text("orphan")
    new = bins.inbox_dir() / "f.txt"
    new.write_text("new document")

    def vanished(path):
        path.unlink()  # the other thread got there first
        raise FileNotFoundError(path)

    monkeypatch.setattr(watcher, "reconcile_stale_processing_file", vanished)
    calls: list = []
    monkeypatch.setattr(watcher, "run_pipeline", _fake_pipeline(calls))
    w._process_existing(new)
    assert calls == [(orphan_dir / "f.txt", "new document")]
