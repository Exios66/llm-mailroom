"""Tokenless API requests are served only on a loopback listener (audit L-2).

``python -m api.main`` refuses an off-loopback bind without a token, but a
direct ``uvicorn api.main:app --host 0.0.0.0`` never runs that check. The
request dependency therefore looks at the ASGI ``server`` scope (the local
address the connection actually arrived on) and answers 503 on token-protected
routes when no live token is configured and that address is not loopback.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

_ENV = (
    "MAILROOM_API_TOKEN",
    "MAILROOM_API_TOKENS",
    "MAILROOM_API_TOKEN_REVOKED",
    "MAILROOM_API_HOST",
)


def _reload_main(monkeypatch, temp_base_dir, **env):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    if "api.main" in sys.modules:
        return importlib.reload(sys.modules["api.main"])
    return importlib.import_module("api.main")


def _request(server, authorization: str | None = None) -> Request:
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "headers": headers, "server": server})


@pytest.mark.parametrize(
    "server",
    [
        ("127.0.0.1", 8000),
        ("127.8.9.10", 8000),
        ("::1", 8000),
        ("::ffff:127.0.0.1", 8000),
        ("localhost", 8000),
    ],
)
def test_tokenless_loopback_listener_is_served(monkeypatch, temp_base_dir, server):
    main = _reload_main(monkeypatch, temp_base_dir)
    main._require_token(_request(server))  # no exception


@pytest.mark.parametrize(
    "server",
    [
        ("192.0.2.10", 8000),
        ("10.0.0.5", 7860),
        ("0.0.0.0", 8000),
        ("2001:db8::1", 8000),
        ("::ffff:192.0.2.10", 8000),
        ("testserver", 80),
        ("/run/mailroom.sock", None),  # unix socket: unknown exposure, fail closed
        None,  # ASGI allows a missing server address
    ],
)
def test_tokenless_off_loopback_listener_gets_503(monkeypatch, temp_base_dir, server):
    main = _reload_main(monkeypatch, temp_base_dir)
    with pytest.raises(HTTPException) as exc:
        main._require_token(_request(server))
    assert exc.value.status_code == 503
    assert "MAILROOM_API_TOKEN" in exc.value.detail


def test_fully_revoked_tokens_count_as_tokenless(monkeypatch, temp_base_dir):
    main = _reload_main(
        monkeypatch,
        temp_base_dir,
        MAILROOM_API_TOKEN="old",
        MAILROOM_API_TOKEN_REVOKED="old",
    )
    with pytest.raises(HTTPException) as exc:
        main._require_token(_request(("192.0.2.10", 8000)))
    assert exc.value.status_code == 503


@pytest.mark.parametrize("server", [("192.0.2.10", 8000), ("127.0.0.1", 8000)])
def test_live_token_rules_are_unchanged_on_any_listener(monkeypatch, temp_base_dir, server):
    main = _reload_main(monkeypatch, temp_base_dir, MAILROOM_API_TOKEN="key")
    with pytest.raises(HTTPException) as exc:
        main._require_token(_request(server))
    assert exc.value.status_code == 401
    main._require_token(_request(server, "Bearer key"))  # no exception


def test_direct_uvicorn_style_app_blocks_protected_routes_not_health(
    monkeypatch, temp_base_dir
):
    """End to end through the app: what ``uvicorn --host 0.0.0.0`` without a token serves."""
    main = _reload_main(monkeypatch, temp_base_dir, MAILROOM_EMBED_WATCHER="0")
    with TestClient(main.app, base_url="http://192.0.2.10:8000") as client:
        assert client.get("/health").status_code == 200
        for path in ("/status/missing", "/v1/status/missing", "/queue"):
            assert client.get(path).status_code == 503, path


def test_loopback_dev_app_still_serves_without_token(monkeypatch, temp_base_dir):
    main = _reload_main(monkeypatch, temp_base_dir, MAILROOM_EMBED_WATCHER="0")
    with TestClient(main.app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/queue").status_code == 200


def test_token_set_after_import_is_live(monkeypatch, temp_base_dir):
    """MAILROOM_API_TOKEN is read per request, like MAILROOM_API_TOKENS.

    local-mailroom-sandbox's offline pilot (``connect_review_api``) imports
    ``api.main`` and only then sets the token, talking to ``http://testserver``
    with a Bearer header. An import-time snapshot would see no token and 503 it.
    """
    main = _reload_main(monkeypatch, temp_base_dir)
    monkeypatch.setenv("MAILROOM_API_TOKEN", "late")
    main._require_token(_request(("testserver", 80), "Bearer late"))  # no exception
    with pytest.raises(HTTPException) as exc:
        main._require_token(_request(("testserver", 80)))
    assert exc.value.status_code == 401
