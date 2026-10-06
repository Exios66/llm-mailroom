"""Run landing-page JavaScript unit tests through the repository's pytest entry point."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_landing_javascript_behaviors():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Landing-page behavioral tests require Node.js >=18")
    tests = Path(__file__).with_suffix(".test.cjs")
    result = subprocess.run(
        [node, "--test", str(tests)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
