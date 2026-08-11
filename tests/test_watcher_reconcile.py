"""Watcher reconciliation tests (audit L-1/A-18): stale processing claims.

No watchdog/filesystem events involved — exercises bins-level reconciliation
helpers and the recover_processing script decision logic.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _touch_old(p: Path):
    old = time.time() - 7200
    os.utime(p, (old, old))


def test_stale_claims_detected(temp_base_dir):
    from pipeline import bins

    proc = bins.processing_dir("worker-abc")
    proc.mkdir(parents=True, exist_ok=True)
    stale_f = proc / "doc.pdf"
    stale_f.write_bytes(b"x")
    _touch_old(stale_f)
    fresh_f = proc / "fresh.pdf"
    fresh_f.write_bytes(b"y")  # current mtime → not stale

    stale = bins.list_stale_processing_files(stale_minutes=60)
    assert stale_f in stale
    assert fresh_f not in stale


def test_requeue_moves_to_inbox(temp_base_dir):
    from pipeline import bins

    proc = bins.processing_dir("worker-abc")
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "doc.pdf"
    f.write_bytes(b"x")
    _touch_old(f)

    dest = bins.requeue_stale_processing(f)
    assert dest == bins.inbox_dir() / "doc.pdf"
    assert dest.exists()
    assert not f.exists()


def test_mark_processing_dead_retires_to_failed(temp_base_dir):
    from pipeline import bins

    proc = bins.processing_dir("worker-abc")
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "doc.pdf"
    f.write_bytes(b"x")

    dest = bins.mark_processing_dead("worker-abc", "doc.pdf")
    assert dest == bins.failed_dir() / "doc.pdf"
    assert dest.exists()
    assert not f.exists()


def test_recover_script_requeues_stale(monkeypatch, temp_base_dir, capsys):
    from pipeline import bins

    proc = bins.processing_dir("worker-abc")
    proc.mkdir(parents=True, exist_ok=True)
    f = proc / "doc.pdf"
    f.write_bytes(b"x")
    _touch_old(f)

    env = dict(os.environ, MAILROOM_BASE_DIR=str(temp_base_dir))
    r = subprocess.run(
        [sys.executable, "scripts/recover_processing.py", "--apply", "--stale-minutes", "60"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "requeue" in r.stdout
    assert (bins.inbox_dir() / "doc.pdf").exists()


class TestPauseTTL:
    """L-4/O-13: pause flag is JSON with actor/reason/expiry and auto-expires."""

    def test_set_and_get_pause_info(self, temp_base_dir):
        from pipeline import bins

        assert bins.set_ingestion_paused(actor="ops_monitor", reason="load spike", ttl_seconds=3600)
        info = bins.get_pause_info()
        assert info["actor"] == "ops_monitor"
        assert info["reason"] == "load spike"
        assert info["expires_at"] > info["set_at"]
        assert bins.is_ingestion_paused()

    def test_ttl_expiry_auto_clears(self, temp_base_dir, monkeypatch):
        from pipeline import bins
        import time

        assert bins.set_ingestion_paused(actor="ops_monitor", reason="x", ttl_seconds=1)
        assert bins.is_ingestion_paused()
        monkeypatch.setattr(bins, "_PAUSE_TTL_SECONDS", 0)  # not used; simulate expiry
        # Simulate time passing: rewrite with an already-expired expires_at.
        import json

        bins._pause_file_path().write_text(json.dumps({
            "actor": "ops_monitor", "reason": "x",
            "set_at": time.time() - 100, "expires_at": time.time() - 50,
        }))
        assert bins.is_ingestion_paused() is False  # expired -> cleared
        assert bins._pause_file_path().exists() is False

    def test_clear_pause(self, temp_base_dir):
        from pipeline import bins

        bins.set_ingestion_paused(actor="api", reason="manual")
        assert bins.clear_ingestion_paused()
        assert bins.is_ingestion_paused() is False


class TestArchiveCollisionSafe:
    """A-20: move_to_archive never overwrites a same-named file."""

    def test_collision_gets_doc_id_suffix(self, temp_base_dir):
        from pipeline import bins

        archive = bins.archive_dir("M1", "contract")
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "doc.pdf").write_bytes(b"original")

        src = temp_base_dir / "doc.pdf"
        src.write_bytes(b"new version")
        dest = bins.move_to_archive(src, "M1", "contract", doc_id="doc-abc")
        assert dest.name == "doc--doc-abc.pdf"
        assert (archive / "doc.pdf").read_bytes() == b"original"  # untouched
        assert dest.read_bytes() == b"new version"

    def test_no_collision_keeps_name(self, temp_base_dir):
        from pipeline import bins

        archive = bins.archive_dir("M1", "contract")
        archive.mkdir(parents=True, exist_ok=True)
        src = temp_base_dir / "fresh.pdf"
        src.write_bytes(b"x")
        dest = bins.move_to_archive(src, "M1", "contract", doc_id="doc-1")
        assert dest.name == "fresh.pdf"
