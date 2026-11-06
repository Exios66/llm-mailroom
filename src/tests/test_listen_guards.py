"""Listen-port / bind guards for container deploys (Railway retired)."""

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
        "SPACE_ID",
        "HF_SPACE_ID",
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


@pytest.mark.parametrize("platform_port", ["", " ", "\t\n"])
@pytest.mark.parametrize("configured_port,expected", [("7860", 7860), (None, 8000)])
def test_blank_platform_port_uses_documented_fallback(
    monkeypatch, platform_port, configured_port, expected
):
    """A present but blank PORT is not a platform port override (PR #171)."""
    main = _reload_main(
        monkeypatch, PORT=platform_port, MAILROOM_API_PORT=configured_port
    )
    assert main.listen_port() == expected


def test_padded_platform_port_still_overrides_configured_port(monkeypatch):
    main = _reload_main(monkeypatch, PORT=" \t8080\n", MAILROOM_API_PORT="7860")
    assert main.listen_port() == 8080


def test_off_loopback_without_token_exits_with_space_hint(monkeypatch):
    main = _reload_main(
        monkeypatch,
        MAILROOM_API_HOST="0.0.0.0",
        SPACE_ID="user/mailroom-producer",
    )
    with pytest.raises(SystemExit) as exc:
        main.assert_bind_allowed()
    msg = str(exc.value)
    assert "MAILROOM_API_TOKEN" in msg
    assert "Hugging Face Spaces" in msg


def test_off_loopback_with_token_allowed(monkeypatch):
    main = _reload_main(
        monkeypatch,
        MAILROOM_API_HOST="0.0.0.0",
        MAILROOM_API_TOKEN="secret",
        SPACE_ID="user/mailroom-producer",
    )
    main.assert_bind_allowed()  # does not raise


def test_railway_is_retired():
    """Railway Config as Code and its nixpacks fallback are gone (The-Mailroom ADR-005)."""
    for rel in ("railway.json", "railway.toml", ".railway", "nixpacks.toml"):
        assert not (REPO_ROOT / rel).exists(), rel
