"""Manifest read-modify-write is serialized and compare-and-set (#303)."""

from __future__ import annotations

import fcntl
import threading

import pytest

from audit.row import mark_manifest, observe_manifest
from pipeline import bins
from schemas.manifest import DocumentManifest


@pytest.fixture
def manifest(temp_base_dir):
    bins.save_manifest(DocumentManifest(doc_id="d-1", matter_id="M", original_filename="d-1.pdf"))
    return "d-1"


def _lock_is_held_by_someone() -> bool:
    """Probe the lock file from a second file description: ``flock`` conflicts even inside one process."""
    with open(bins.manifests_dir() / bins._MANIFEST_LOCK_NAME, "a+b") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


def test_update_manifest_holds_the_file_lock_only_while_it_runs(manifest):
    seen = []

    def mutate(m):
        seen.append(_lock_is_held_by_someone())
        m.audit_written = True

    assert bins.update_manifest(manifest, mutate) is True
    assert seen == [True]
    assert not _lock_is_held_by_someone()
    assert bins.load_manifest(manifest).audit_written is True


def test_update_manifest_sees_the_manifest_as_it_is_on_disk_now(manifest):
    stale = bins.load_manifest(manifest)  # a caller's earlier view
    other = bins.load_manifest(manifest)
    other.audit_written = True
    bins.save_manifest(other)
    seen = []
    assert bins.update_manifest(manifest, lambda m: seen.append(m.audit_written) or False) is False
    assert stale.audit_written is None and seen == [True]  # the callback got the fresh state and declined


def test_update_manifest_declines_and_missing_manifest_are_not_saves(manifest):
    assert bins.update_manifest("nope", lambda m: True) is False
    before = (bins.manifests_dir() / "d-1.json").read_text()
    assert bins.update_manifest(manifest, lambda m: False) is False
    assert (bins.manifests_dir() / "d-1.json").read_text() == before


def test_the_lock_is_reentrant_in_one_thread(manifest):
    with bins.manifest_lock():
        assert bins.update_manifest(manifest, lambda m: setattr(m, "audit_written", True)) is True
    assert not _lock_is_held_by_someone()
    assert bins._manifest_lock_depth == 0


def test_a_second_thread_waits_for_the_lock(manifest):
    inside = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def holder():
        with bins.manifest_lock():
            order.append("holder-in")
            inside.set()
            assert release.wait(5)
            order.append("holder-out")

    def waiter():
        with bins.manifest_lock():
            order.append("waiter-in")

    t1 = threading.Thread(target=holder)
    t1.start()
    assert inside.wait(5)
    t2 = threading.Thread(target=waiter)
    t2.start()
    assert _lock_is_held_by_someone()
    release.set()
    t1.join(5)
    t2.join(5)
    assert order == ["holder-in", "holder-out", "waiter-in"]


def test_without_fcntl_the_update_still_runs_under_the_process_lock(manifest, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_fcntl(name, *a, **kw):
        if name == "fcntl":
            raise ImportError("no fcntl here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    monkeypatch.setattr(bins, "_manifest_lock_warned", False)
    assert bins.update_manifest(manifest, lambda m: setattr(m, "audit_written", True)) is True
    assert bins.load_manifest(manifest).audit_written is True


def test_an_unlockable_lock_file_does_not_stop_the_update(manifest, monkeypatch):
    monkeypatch.setattr(bins, "_manifest_lock_warned", False)
    monkeypatch.setattr(fcntl, "flock", lambda *a, **kw: (_ for _ in ()).throw(OSError("no locks")))
    assert bins.update_manifest(manifest, lambda m: setattr(m, "audit_written", True)) is True
    assert bins._manifest_lock_depth == 0


def test_mark_manifest_expect_applies_only_to_the_observed_state(manifest):
    observed = observe_manifest(manifest)
    assert mark_manifest(manifest, "pending") is True  # another writer moves the manifest
    assert mark_manifest(manifest, "pending_partial", [{"sink": "s", "ref": "r"}], expect=observed) is False
    assert bins.load_manifest(manifest).audit_written == "pending"
    assert mark_manifest(manifest, "pending_partial", [{"sink": "s", "ref": "r"}],
                         expect=observe_manifest(manifest)) is True
    assert bins.load_manifest(manifest).audit_written == "pending_partial"


def test_mark_manifest_expect_compares_the_refs_too(manifest):
    mark_manifest(manifest, "pending_partial", [{"sink": "a", "ref": "1"}])
    observed = observe_manifest(manifest)
    mark_manifest(manifest, "pending_partial", [{"sink": "b", "ref": "2"}])
    assert mark_manifest(manifest, "written", expect=observed) is False
    assert bins.load_manifest(manifest).audit_pending_refs == [{"sink": "b", "ref": "2"}]


def test_mark_manifest_without_a_manifest_is_a_quiet_no_op(temp_base_dir):
    assert mark_manifest("ghost", "written") is False
    assert observe_manifest("ghost") is None
