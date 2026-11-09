"""Malformed artifacts cannot turn a successful run into invalid machine JSON."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from mailroom_diagnostics.cli import main
from mailroom_diagnostics.snapshot import _git_state, _repo_root


@pytest.mark.parametrize("confidence", [
    {"high": float("nan")}, {"high": float("inf")}, {"high": True},
    {"high": "not-a-number"}, {"by_class": []}, [],
])
def test_malformed_confidence_is_json_error_not_success(confidence, minimal_contract, tmp_path, capsys):
    data = json.loads(minimal_contract.read_text())
    data["confidence"] = confidence
    path = tmp_path / "bad-confidence.json"
    path.write_text(json.dumps(data))
    assert main(["inspect", "--contract", str(path), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out, parse_constant=_reject_constant)
    assert payload["ok"] is False


def _reject_constant(value):
    raise ValueError(f"not JSON: {value}")


def test_numeric_overflow_is_rejected(minimal_contract, tmp_path, capsys):
    data = json.loads(minimal_contract.read_text())
    data["confidence"] = {"high": "overflow-marker"}
    path = tmp_path / "overflow.json"
    path.write_text(json.dumps(data).replace('"overflow-marker"', "1e999"))
    assert main(["inspect", "--contract", str(path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_graph_does_not_coerce_text_to_boolean(minimal_contract, tmp_path, capsys):
    data = json.loads(minimal_contract.read_text())
    data["graph"]["edges"][0]["conditional"] = "false"
    path = tmp_path / "coerced.json"
    path.write_text(json.dumps(data))
    assert main(["check", "--contract", str(path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid-schema"


def test_invalid_cli_arguments_still_have_machine_error(capsys):
    assert main(["check", "--json", "--bogus"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid-arguments"


def test_untracked_source_is_dirty_provenance(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True)
    git("init", "-q")
    (tmp_path / "tracked.txt").write_text("baseline")
    git("add", "tracked.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "baseline")
    assert _git_state(tmp_path)[1] is False
    (tmp_path / "new_source.py").write_text("value = 1\n")
    assert _git_state(tmp_path)[1] is True


def test_wheel_inside_a_git_checkout_does_not_inherit_its_sha(tmp_path, monkeypatch):
    import pipeline
    (tmp_path / ".git").mkdir()
    installed = tmp_path / ".venv/lib/python3.12/site-packages/pipeline/__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    monkeypatch.setattr(pipeline, "__file__", str(installed))
    assert _repo_root() is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_artifact_fifo_is_rejected_without_waiting_for_a_writer(tmp_path, cli_env):
    fifo = tmp_path / "input.json"
    os.mkfifo(fifo)
    run = subprocess.run([sys.executable, "-m", "mailroom_diagnostics", "inspect",
                          "--contract", str(fifo), "--json"], env=cli_env,
                         capture_output=True, text=True, timeout=3)
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "io-error"
