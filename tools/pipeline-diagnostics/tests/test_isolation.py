"""Isolation: no pipeline graph for artifacts, no worker/API imports, no leaks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mailroom_diagnostics.snapshot import build_workspace_snapshot, load_artifact

REPO_ROOT = Path(__file__).resolve().parents[3]

_ARTIFACT_FORBIDDEN = (
    "graph",
    "graph.build_graph",
    "api.main",
    "pipeline",
    "pipeline.config",
    "pipeline.watcher",
    "pipeline.gmail_intake",
)
_WORKSPACE_FORBIDDEN = ("api.main", "pipeline.watcher", "pipeline.gmail_intake")


def _run_probe(code: str, *args: str, env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_ARTIFACT_PROBE = """
import contextlib, io, json, sys
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    from mailroom_diagnostics.cli import main
    rc = main(["inspect", "--contract", sys.argv[1], "--json"])
payload = json.loads(buf.getvalue())
loaded = sorted(m for m in sys.modules if m in sys.argv[2].split(","))
print(json.dumps({"rc": rc, "loaded": loaded, "kind": payload["provenance"]["kind"]}))
"""

_WORKSPACE_PROBE = """
import contextlib, io, json, sys
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    from mailroom_diagnostics.cli import main
    rc = main(["inspect", "--json"])
payload = json.loads(buf.getvalue())
loaded = sorted(m for m in sys.modules if m in sys.argv[1].split(","))
print(json.dumps({"rc": rc, "loaded": loaded, "kind": payload["provenance"]["kind"]}))
"""


def test_artifact_mode_does_not_import_the_pipeline_graph(cli_env, contract_path):
    result = _run_probe(
        _ARTIFACT_PROBE, str(contract_path), ",".join(_ARTIFACT_FORBIDDEN), env=cli_env
    )
    assert result == {"rc": 0, "loaded": [], "kind": "artifact"}


def test_workspace_mode_does_not_import_the_api_or_watcher(cli_env):
    # The graph itself is compiled in this mode, so only the worker/API
    # entrypoints are forbidden.
    result = _run_probe(_WORKSPACE_PROBE, ",".join(_WORKSPACE_FORBIDDEN), env=cli_env)
    assert result["rc"] == 0
    assert result["kind"] == "workspace-preview"
    assert result["loaded"] == []


def test_importing_the_tool_has_no_side_effects(cli_env):
    code = (
        "import sys, mailroom_diagnostics, mailroom_diagnostics.cli\n"
        "print(int('graph' in sys.modules), int('api.main' in sys.modules))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=cli_env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0 0"


def test_help_does_not_compile_the_graph(cli_env):
    proc = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=cli_env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert "inspect" in proc.stdout and "check" in proc.stdout


def _walk(value, path=""):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


@pytest.mark.parametrize("mode", ["workspace", "artifact"])
def test_snapshot_exports_no_secrets_or_absolute_paths(mode, contract_path):
    snapshot = (
        build_workspace_snapshot() if mode == "workspace" else load_artifact(contract_path)
    )
    payload = json.loads(snapshot.model_dump_json())
    blob = json.dumps(payload)
    for forbidden in (str(REPO_ROOT), "/Users/", "/private/", "/home/", "sk-", "OPENROUTER"):
        assert forbidden not in blob
    for path, value in _walk(payload):
        key = path.rsplit(".", 1)[-1].lower()
        assert not any(word in key for word in ("secret", "token", "password", "api_key"))
        if isinstance(value, str) and value.startswith("/"):
            raise AssertionError(f"absolute path exported at {path}: {value}")


def test_workspace_snapshot_builds_no_model_client(monkeypatch):
    """Compiling the graph must not construct an LLM client or make a call."""
    from llm import client as llm_client

    def boom(*args, **kwargs):
        raise AssertionError("a model client was constructed while inspecting the graph")

    monkeypatch.setattr(llm_client, "OpenAI", boom)
    monkeypatch.setattr(llm_client, "get_llm", boom)
    snapshot = build_workspace_snapshot()
    assert snapshot.graph.nodes


def test_provenance_distinguishes_workspace_from_artifact(contract_path):
    workspace = build_workspace_snapshot().provenance
    artifact = load_artifact(contract_path).provenance
    assert workspace.kind == "workspace-preview"
    assert workspace.active_worker is False
    assert workspace.git_sha is None or len(workspace.git_sha) == 40
    assert artifact.kind == "artifact"
    assert artifact.active_worker is False
    assert artifact.git_sha is None
    assert artifact.pipeline_version  # copied from the artifact, not recompiled
