"""Standalone diagnostics app: auth parity, inert imports, honest failures."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def auth_headers(monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "diag-token")
    return {"Authorization": "Bearer diag-token"}


@pytest.fixture
def client(auth_headers):
    from mailroom_diagnostics.app import create_app

    return TestClient(create_app(), headers=auth_headers)


def test_capabilities_are_honest_and_read_only(client):
    response = client.get("/v1/diagnostics/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert body["source_modes"] == ["workspace-preview"]
    features = body["features"]
    assert features["graph"] is True
    assert features["structural_checks"] is True
    assert features["runs"] is True
    assert features["experiments"] is True
    assert features["replay"] == "cli"
    for unavailable in ("live_events", "calibration", "bundles"):
        assert features[unavailable] is False
    assert body["mount"] == "/diagnostics"


def test_routes_require_the_producer_token(monkeypatch, auth_headers):
    from mailroom_diagnostics.app import create_app

    anonymous = TestClient(create_app())  # token configured, no header
    assert anonymous.get("/v1/diagnostics/capabilities").status_code == 401
    assert anonymous.get("/v1/diagnostics/snapshot").status_code == 401


def test_tokenless_non_loopback_listener_is_refused(monkeypatch):
    for name in (
        "MAILROOM_API_TOKEN",
        "MAILROOM_API_TOKENS",
        "MAILROOM_API_TOKEN_REVOKED",
    ):
        monkeypatch.delenv(name, raising=False)

    from mailroom_diagnostics.app import create_app

    anonymous = TestClient(create_app())
    response = anonymous.get("/v1/diagnostics/capabilities")
    assert response.status_code == 503
    assert "API token not configured" in response.text


def test_snapshot_is_a_labelled_workspace_preview(client):
    response = client.get("/v1/diagnostics/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["provenance"]["kind"] == "workspace-preview"
    assert body["provenance"]["active_worker"] is False
    assert body["provenance"]["tracing"] == "disabled"
    assert body["graph"]["nodes"] and body["graph"]["edges"]
    assert body["graph_hash"].startswith("sha256:")


def test_findings_returns_the_check_report(client):
    response = client.get("/v1/diagnostics/findings")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"]
    assert isinstance(body["findings"], list)
    assert body["exit_code"] in (0, 1)


def test_graph_failure_is_sanitized(client, monkeypatch):
    from mailroom_diagnostics import api as diagnostics_api
    from mailroom_diagnostics.models import DiagnosticsError

    def boom():
        raise DiagnosticsError("graph-construction-failed", "unable to compile the pipeline graph")

    monkeypatch.setattr(diagnostics_api, "build_workspace_snapshot", boom)
    response = client.get("/v1/diagnostics/snapshot")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "graph-construction-failed"
    assert "/Users/" not in response.text
    assert str(REPO_ROOT) not in response.text


def test_unexpected_failure_is_generic(auth_headers, monkeypatch):
    from mailroom_diagnostics import api as diagnostics_api

    def boom():
        raise RuntimeError(f"private path {REPO_ROOT}")

    monkeypatch.setattr(diagnostics_api, "build_workspace_snapshot", boom)
    from mailroom_diagnostics.app import create_app

    tolerant = TestClient(create_app(), headers=auth_headers, raise_server_exceptions=False)
    response = tolerant.get("/v1/diagnostics/snapshot")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "runtime-error"
    assert str(REPO_ROOT) not in response.text


def test_missing_ui_serves_build_instructions(client):
    response = client.get("/diagnostics")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "not built in this checkout" in response.text
    assert "/v1/diagnostics/snapshot" in response.text


def test_health_is_public_and_reports_ui_state(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tool"] == "mailroom-diagnostics"


def test_openapi_is_exposed_under_the_diagnostics_prefix(client):
    response = client.get("/v1/diagnostics/openapi.json")
    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert {
        "/v1/diagnostics/capabilities",
        "/v1/diagnostics/snapshot",
        "/v1/diagnostics/findings",
    } <= paths


def test_app_import_does_not_pull_the_producer_or_watcher(cli_env):
    """Constructing the app must not import api.main or start any worker."""
    code = (
        "import json, sys\n"
        "import mailroom_diagnostics.app as app\n"
        "app.create_app()\n"
        "names = ['api.main', 'pipeline.watcher', 'pipeline.gmail_intake',\n"
        "         'pipeline.status_notify', 'graph.build_graph', 'llm.client']\n"
        "print(json.dumps(sorted(name for name in sys.modules if name in set(names))))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=cli_env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_serve_help_exposes_bind_options(run_cli):
    proc = run_cli("serve", "--help")
    assert proc.returncode == 0
    assert "--host" in proc.stdout
    assert "--port" in proc.stdout


def test_prepare_bind_loads_repository_env_before_the_token_check(monkeypatch):
    from mailroom_diagnostics import app as app_module

    calls = []

    def fake_load_env():
        calls.append("load_env")
        monkeypatch.setenv("MAILROOM_API_TOKEN", "from-dotenv")

    monkeypatch.setattr("pipeline.env.load_env", fake_load_env)
    for name in ("MAILROOM_API_TOKENS", "MAILROOM_API_TOKEN_REVOKED"):
        monkeypatch.delenv(name, raising=False)
    app_module.prepare_bind("0.0.0.0")  # no SystemExit
    assert calls == ["load_env"]


def test_prepare_bind_still_refuses_off_loopback_without_a_token(monkeypatch):
    from mailroom_diagnostics import app as app_module

    monkeypatch.setattr("pipeline.env.load_env", lambda: None)
    for name in ("MAILROOM_API_TOKEN", "MAILROOM_API_TOKENS", "MAILROOM_API_TOKEN_REVOKED"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        app_module.prepare_bind("0.0.0.0")
