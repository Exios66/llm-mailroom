"""`cutover.py --validate` must point pytest at a directory that exists."""

from pathlib import Path
from types import SimpleNamespace

from scripts import cutover

ROOT = Path(__file__).resolve().parents[2]


def test_validate_agent_targets_existing_agent_tests(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cutover.subprocess, "run", fake_run)
    cutover.validate_agent("sorter")
    assert (ROOT / calls[0][3]).is_dir()
