"""Smoke test for the scale hill-climb harness (src/scripts/stress_test.py)."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_stress_harness_smoke(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "MAILROOM_BASE_DIR": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, str(ROOT / "src/scripts/stress_test.py"), "--docs", "4", "--threads", "1",
         "--scenarios", "throughput,db_contention", "--db-threads", "4"],
        capture_output=True, text=True, env=env, timeout=240,
    )
    assert out.returncode in (0, 2), out.stderr[-2000:]
    lines = out.stdout.strip().splitlines()
    assert lines[-1].startswith("SCORE: ")
    float(lines[-1].split()[1])
    assert "GATE db_failed_ops PASS" in out.stdout
