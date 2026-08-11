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
