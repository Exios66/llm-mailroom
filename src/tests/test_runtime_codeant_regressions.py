"""Source PR118 CodeAnt regressions: ownership, no-clobber parking and locking."""

import errno
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


def _review_manifest(**kwargs):
    from schemas.manifest import DocumentManifest, PipelineStage

    return DocumentManifest(doc_id="OWNED", matter_id="M", original_filename="same.txt",
                            stage=PipelineStage.REVIEW, doc_type="contract", **kwargs)


def _unsafe_sibling(doc_id):
    from pipeline.bins import manifests_dir

    data = _review_manifest().model_dump(mode="json")
    data["doc_id"] = doc_id
    (manifests_dir() / "broken.json").write_text(json.dumps(data))


@pytest.mark.parametrize("unsafe_id", ["../escape", "a\\b", "C:escape", "bad\0id"])
def test_unsafe_sibling_fails_closed_but_owned_path_remains_usable(temp_base_dir, unsafe_id):
    from pipeline.bins import review_dir, review_file_path

    manifest = _review_manifest()
    legacy = review_dir() / "same.txt"
    legacy.write_bytes(b"unverified legacy document")
    _unsafe_sibling(unsafe_id)
    owned = review_dir() / "same--OWNED.txt"
    assert review_file_path(manifest) == owned
    assert not owned.exists()
    assert legacy.read_bytes() == b"unverified legacy document"
    owned.write_bytes(b"verified owned document")
    assert review_file_path(manifest).read_bytes() == b"verified owned document"


@pytest.mark.parametrize("unsafe_id", ["../escape", "C:escape"])
def test_complete_api_denies_unsafe_legacy_then_archives_owned_bytes(
    temp_base_dir, monkeypatch, unsafe_id,
):
    """Real HTTP routing, SQLite/audit writes and completion; no mocked handler."""
    from fastapi.testclient import TestClient
    from pipeline.bins import archive_dir, load_manifest, review_dir, save_manifest
    from storage.db import ensure_schema

    monkeypatch.setenv("MAILROOM_API_TOKEN", "codeant-test-token")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    import api.main

    api = importlib.reload(api.main)
    ensure_schema()
    manifest = _review_manifest()
    save_manifest(manifest)
    legacy = review_dir() / "same.txt"
    legacy.write_bytes(b"unverified legacy document")
    _unsafe_sibling(unsafe_id)
    payload = {"decision": "approved", "disposition": "complete",
               "extracted_data": {"parties": ["Acme"], "confidence": 0.99}}
    headers = {"Authorization": "Bearer codeant-test-token"}
    with TestClient(api.app, raise_server_exceptions=False) as client:
        denied = client.post("/review/OWNED/resolve", json=payload, headers=headers)
        assert denied.status_code == 404, denied.text
        assert "File not found in review bin" in denied.json()["detail"]
        assert load_manifest("OWNED").stage.value == "review"
        assert legacy.read_bytes() == b"unverified legacy document"
        owned = review_dir() / "same--OWNED.txt"
        owned.write_bytes(b"verified owned document")
        completed = client.post("/review/OWNED/resolve", json=payload, headers=headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["complete"]["stage"] == "archived"
    assert not owned.exists()
    assert (archive_dir("M", "contract") / "same--OWNED.txt").read_bytes() == b"verified owned document"
    assert legacy.read_bytes() == b"unverified legacy document"
    saved = load_manifest("OWNED")
    assert saved.stage.value == "archived" and saved.extracted_data["parties"] == ["Acme"]


@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("identical", [False, True])
def test_independent_review_workers_publish_once_without_clobber(
    temp_base_dir, tmp_path, cross_device, identical,
):
    """Both workers observe absence before actual filesystem publication.

    EXDEV is injected only at the source hard link; destination staging,
    copying and no-clobber linking remain real filesystem operations.
    """
    from pipeline.bins import load_manifest, review_dir

    program = r'''
import errno, json, os, sys, time
from pathlib import Path
from pipeline import bins
from schemas.manifest import DocumentManifest, PipelineStage
source, ready, release = map(Path, sys.argv[1:4])
cross_device = sys.argv[4] == "True"
manifest = DocumentManifest(doc_id="OWNED", matter_id="M", original_filename="same.txt",
                            stage=PipelineStage.REVIEW)
dest = bins.review_dir() / "same--OWNED.txt"
real_is_file, real_link = Path.is_file, os.link
synced = False
def synchronized_is_file(path):
    global synced
    exists = real_is_file(path)
    if path == dest and not synced:
        synced = True
        ready.write_text(str(exists))
        deadline = time.monotonic() + 15
        while not release.exists():
            assert time.monotonic() < deadline, "publication barrier timed out"
            time.sleep(.005)
    return exists
def link_with_cross_device_boundary(src, target, *args, **kwargs):
    if cross_device and Path(src) == source:
        raise OSError(errno.EXDEV, "injected cross-device boundary")
    # Actual destination publication must contain complete source bytes.
    assert Path(src).read_bytes() == source.read_bytes()
    return real_link(src, target, *args, **kwargs)
Path.is_file, os.link = synchronized_is_file, link_with_cross_device_boundary
try:
    path, newly = bins.park_for_review(source, manifest)
    result = {"status": "ok", "newly": newly, "path": str(path)}
except FileExistsError as exc:
    result = {"status": "collision", "error": str(exc)}
print("RESULT:" + json.dumps(result), flush=True)
'''
    sources = [tmp_path / f"source-{index}.txt" for index in range(2)]
    contents = [b"FIRST independent legal document", b"SECOND distinct legal document"]
    if identical:
        contents[1] = contents[0]
    for source, content in zip(sources, contents):
        source.write_bytes(content)
    ready = [tmp_path / f"ready-{index}" for index in range(2)]
    release = tmp_path / "release"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               PYTHONDONTWRITEBYTECODE="1")
    workers = [subprocess.Popen(
        [sys.executable, "-c", program, str(source), str(signal), str(release), str(cross_device)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for source, signal in zip(sources, ready)]
    results = []
    try:
        deadline = time.monotonic() + 15
        while not all(path.exists() for path in ready):
            assert time.monotonic() < deadline, "workers did not reach publication barrier"
            assert all(worker.poll() is None for worker in workers), "worker exited before barrier"
            time.sleep(.005)
        assert [path.read_text() for path in ready] == ["False", "False"]
        release.touch()
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stdout + stderr
            results.append(json.loads(next(line[7:] for line in stdout.splitlines()
                                           if line.startswith("RESULT:"))))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    winners = [index for index, result in enumerate(results)
               if result["status"] == "ok" and result["newly"]]
    assert len(winners) == 1, results
    winner = winners[0]
    loser = 1 - winner
    dest = review_dir() / "same--OWNED.txt"
    assert dest.read_bytes() == contents[winner]
    assert not sources[winner].exists()
    assert sources[loser].read_bytes() == contents[loser]
    if identical:
        assert results[loser] == {"status": "ok", "newly": False, "path": str(dest)}
    else:
        assert results[loser]["status"] == "collision"
        assert "Different bytes already parked" in results[loser]["error"]
    assert load_manifest("OWNED").doc_id == "OWNED"
    assert sorted(path.name for path in review_dir().iterdir()) == [dest.name]


def test_review_existing_collision_and_identical_replay_preserve_sources(temp_base_dir):
    from pipeline.bins import park_for_review, review_dir

    manifest = _review_manifest()
    source = temp_base_dir / "source.txt"
    source.write_bytes(b"winner")
    dest, newly = park_for_review(source, manifest)
    assert newly and dest.read_bytes() == b"winner" and not source.exists()
    assert park_for_review(source, manifest) == (dest, False)
    source.write_bytes(b"winner")
    assert park_for_review(source, manifest) == (dest, False)
    assert source.read_bytes() == b"winner"
    source.write_bytes(b"different")
    with pytest.raises(FileExistsError, match="Different bytes already parked"):
        park_for_review(source, manifest)
    assert source.read_bytes() == b"different" and dest.read_bytes() == b"winner"
    assert sorted(path.name for path in review_dir().iterdir()) == [dest.name]


@pytest.mark.parametrize("failure", ["source_link", "copy", "publication_link"])
def test_failed_review_publication_keeps_source_and_removes_staging(temp_base_dir, monkeypatch, failure):
    from pipeline import bins

    source = temp_base_dir / "source.txt"
    source.write_bytes(b"complete private legal document")
    real_link, real_copy = os.link, bins.shutil.copy2

    def link(src, dest, *args, **kwargs):
        if Path(src) == source:
            code = errno.EPERM if failure == "source_link" else errno.EXDEV
            raise OSError(code, "injected source link failure")
        if failure == "publication_link":
            assert Path(src).read_bytes() == source.read_bytes()
            raise OSError(errno.EPERM, "injected publication link failure")
        return real_link(src, dest, *args, **kwargs)

    def copy(src, dest, *args, **kwargs):
        if failure == "copy":
            Path(dest).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "injected copy failure")
        return real_copy(src, dest, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(bins.shutil, "copy2", copy)
    with pytest.raises(OSError, match="injected"):
        bins.park_for_review(source, _review_manifest())
    assert source.read_bytes() == b"complete private legal document"
    assert list(bins.review_dir().iterdir()) == []
    assert list(bins.manifests_dir().iterdir()) == []


def _windows_lock_double(monkeypatch, locking):
    """Exercise the Windows branch on real files without changing global os.name."""
    from storage import warehouse

    monkeypatch.setattr(warehouse, "os", SimpleNamespace(name="nt", SEEK_END=os.SEEK_END))
    monkeypatch.setitem(sys.modules, "msvcrt",
                        SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0, locking=locking))
    return warehouse


def test_windows_publication_lock_waits_beyond_ten_contentions(tmp_path, monkeypatch):
    attempts, sleeps, released = [], [], []

    def locking(fd, mode, count):
        assert count == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert os.fstat(fd).st_size == 1
        if mode == 0:
            released.append(fd)
        elif mode == 1:
            raise OSError(errno.EDEADLK, "CRT blocking lock exhausted ten retries")
        else:
            attempts.append(fd)
            if len(attempts) <= 25:
                raise OSError(errno.EACCES, "another exporter holds the byte")

    warehouse = _windows_lock_double(monkeypatch, locking)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with warehouse._publication_lock(tmp_path):
        assert len(attempts) == 26 and len(sleeps) == 25
        assert not released
        (tmp_path / "published").write_bytes(b"transaction ran after lock acquisition")
    assert len(released) == 1
    assert all(delay > 0 for delay in sleeps)
    assert (tmp_path / "published").read_bytes() == b"transaction ran after lock acquisition"


@pytest.mark.parametrize("code", [errno.EBADF, errno.EINVAL, errno.EIO, errno.EPERM])
def test_windows_lock_does_not_retry_noncontention_errors(tmp_path, monkeypatch, code):
    calls = []

    def locking(fd, mode, count):
        calls.append(mode)
        raise OSError(code, "noncontention lock failure")

    warehouse = _windows_lock_double(monkeypatch, locking)
    with pytest.raises(OSError, match="noncontention") as error:
        with warehouse._publication_lock(tmp_path):
            pytest.fail("transaction must not start without a lock")
    assert error.value.errno == code and len(calls) == 1


def test_windows_lock_released_when_transaction_raises(tmp_path, monkeypatch):
    calls = []
    warehouse = _windows_lock_double(monkeypatch, lambda fd, mode, count: calls.append(mode))
    with pytest.raises(RuntimeError, match="transaction failure"):
        with warehouse._publication_lock(tmp_path):
            raise RuntimeError("transaction failure")
    assert len(calls) == 2 and calls[-1] == 0
