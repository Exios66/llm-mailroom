"""Listen-port / bind guards for Railway + container deploys."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _reload_main(monkeypatch, **env):
    for key in (
        "PORT",
        "MAILROOM_API_PORT",
        "MAILROOM_API_HOST",
        "MAILROOM_API_TOKEN",
        "MAILROOM_API_TOKENS",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    if "api.main" in sys.modules:
        return importlib.reload(sys.modules["api.main"])
    return importlib.import_module("api.main")


def test_platform_port_wins_over_image_default(monkeypatch):
    main = _reload_main(
        monkeypatch,
        PORT="8080",
        MAILROOM_API_PORT="7860",
        MAILROOM_API_TOKEN="tok",
    )
    assert main.listen_port() == 8080


def test_mailroom_api_port_used_when_platform_port_absent(monkeypatch):
    main = _reload_main(
        monkeypatch,
        MAILROOM_API_PORT="7860",
        MAILROOM_API_TOKEN="tok",
    )
    assert main.listen_port() == 7860


def test_default_port_is_8000(monkeypatch):
    main = _reload_main(monkeypatch, MAILROOM_API_TOKEN="tok")
    assert main.listen_port() == 8000


def test_off_loopback_without_token_exits_with_railway_hint(monkeypatch):
    main = _reload_main(
        monkeypatch,
        MAILROOM_API_HOST="0.0.0.0",
        RAILWAY_ENVIRONMENT="production",
    )
    with pytest.raises(SystemExit) as exc:
        main.assert_bind_allowed()
    msg = str(exc.value)
    assert "MAILROOM_API_TOKEN" in msg
    assert "Railway" in msg


def test_off_loopback_with_token_allowed(monkeypatch):
    main = _reload_main(
        monkeypatch,
        MAILROOM_API_HOST="0.0.0.0",
        MAILROOM_API_TOKEN="secret",
        RAILWAY_ENVIRONMENT="production",
    )
    main.assert_bind_allowed()  # does not raise


def test_railway_json_forces_dockerfile_builder():
    import json

    cfg = json.loads((REPO_ROOT / "railway.json").read_text(encoding="utf-8"))
    assert cfg["build"]["builder"] == "DOCKERFILE"
    assert cfg["build"]["dockerfilePath"] == "Dockerfile"
    assert cfg["deploy"]["healthcheckPath"] == "/health"
