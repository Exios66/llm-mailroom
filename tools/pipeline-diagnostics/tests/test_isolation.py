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


_GUARD_SOURCE = '''
import builtins
import os
import socket
from pathlib import Path

_marker = Path(os.environ["DIAG_CHILD_MARKER"])


def _record(kind):
    with _marker.open("a", encoding="utf-8") as handle:
        handle.write(kind + "\\n")


def _blocked(*args, **kwargs):
    _record("network")
    raise RuntimeError("network access is disabled in diagnostics probes")


socket.socket.connect = _blocked
socket.create_connection = _blocked

_real_import = builtins.__import__


def _guarded_import(name, *args, **kwargs):
    if name == "openai" or name.startswith(("graph", "pipeline", "api.")):
        _record("import:" + name)
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _guarded_import
'''


@pytest.fixture
def guarded_env(cli_env, tmp_path):
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_GUARD_SOURCE, encoding="utf-8")
    marker = tmp_path / "attempts.txt"
    environment = dict(cli_env, PYTHONPATH=str(guard), DIAG_CHILD_MARKER=str(marker))
    return environment, marker


def _attempts(marker: Path) -> str:
    return marker.read_text(encoding="utf-8") if marker.exists() else ""


def test_child_guard_records_network_attempts(guarded_env):
    environment, marker = guarded_env
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n"
        "except Exception:\n"
        "    pass\n"
    )
    subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=environment, timeout=60, check=False
    )
    assert "network" in _attempts(marker)


def test_help_does_not_import_graph_or_pipeline(guarded_env):
    environment, marker = guarded_env
    proc = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    attempts = _attempts(marker)
    assert "import:graph" not in attempts
    assert "import:pipeline" not in attempts
    assert "network" not in attempts


def test_workspace_probe_imports_graph_without_network(guarded_env):
    environment, marker = guarded_env
    proc = subprocess.run(
        [sys.executable, "-m", "mailroom_diagnostics", "inspect", "--json"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    attempts = _attempts(marker)
    assert "import:graph" in attempts
    assert "network" not in attempts


def test_workspace_probe_never_constructs_a_model_client(guarded_env):
    environment, marker = guarded_env
    code = '''
import builtins
import contextlib
import io
import json
import os
from pathlib import Path

marker = Path(os.environ["DIAG_CHILD_MARKER"])
_real_import = builtins.__import__


def guarded(name, *args, **kwargs):
    module = _real_import(name, *args, **kwargs)
    if name == "openai":
        original = module.OpenAI

        class Guarded(original):
            def __init__(self, *a, **k):
                with marker.open("a", encoding="utf-8") as handle:
                    handle.write("openai-client\\n")
                raise RuntimeError("client construction blocked")

        module.OpenAI = Guarded
    return module


builtins.__import__ = guarded
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    from mailroom_diagnostics.cli import main
    rc = main(["inspect", "--json"])
print(json.dumps({"rc": rc}))
'''
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rc"] == 0
    assert "openai-client" not in _attempts(marker)
