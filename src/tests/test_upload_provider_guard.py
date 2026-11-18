"""Uploads are refused while no LLM provider resolves, when the deployment asks for it.

Without the guard, a producer whose model key is missing accepts every upload and
each document quietly parks in review. ``MAILROOM_UPLOAD_REQUIRE_PROVIDER`` (on in
``deploy/docker-compose.producer.yml``) turns that into an explicit 503.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

AUTH = {"Authorization": "Bearer test-token-123"}


@pytest.fixture
def client(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    if "api.main" in sys.modules:
        importlib.reload(sys.modules["api.main"])
    from api.main import app

    with TestClient(app) as c:
        yield c


def _no_provider(monkeypatch):
    def boom(agent_name):
        raise ValueError("OpenRouter API key not set: sk-or-secret-looking-detail")

    monkeypatch.setattr("llm.client.resolve_agent_model", boom)


def _upload(client, path="/upload", headers=AUTH):
    return client.post(path, files={"file": ("a.txt", b"hello", "text/plain")}, headers=headers)


@pytest.mark.parametrize("path", ["/upload", "/v1/upload"])
def test_guard_on_refuses_uploads_with_503_when_no_provider_resolves(client, monkeypatch, path):
    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "1")
    _no_provider(monkeypatch)
    r = _upload(client, path)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "LLM provider" in detail and "ValueError" in detail
    assert "sk-or" not in detail  # the exception text never reaches the client
    from pipeline.bins import inbox_dir

    assert not any(inbox_dir().iterdir())  # nothing was queued


def test_guard_on_accepts_uploads_when_the_provider_resolves(client, monkeypatch):
    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "true")
    monkeypatch.setattr("llm.client.resolve_agent_model", lambda agent_name: object())
    monkeypatch.setattr("llm.client.check_spend_guardrail", lambda agent_name, resolved: None)
    assert _upload(client).status_code == 202


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_guard_off_keeps_accepting_uploads_without_a_provider(client, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", value)
    _no_provider(monkeypatch)
    assert _upload(client).status_code == 202


def test_producer_compose_turns_the_guard_on():
    compose = (REPO_ROOT / "deploy" / "docker-compose.producer.yml").read_text(encoding="utf-8")
    assert 'MAILROOM_UPLOAD_REQUIRE_PROVIDER: "${MAILROOM_UPLOAD_REQUIRE_PROVIDER:-1}"' in compose


def test_auth_is_checked_before_the_provider_gate(client, monkeypatch):
    """An unauthenticated caller never learns the provider state."""
    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "1")
    _no_provider(monkeypatch)
    assert _upload(client, headers={}).status_code in (401, 403)


def test_guard_on_refuses_a_model_the_free_only_guardrail_blocks(client, monkeypatch):
    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "1")
    monkeypatch.setattr("llm.client.resolve_agent_model", lambda agent_name: object())

    def blocked(agent_name, resolved):
        raise RuntimeError("paid model blocked")

    monkeypatch.setattr("llm.client.check_spend_guardrail", blocked)
    r = _upload(client)
    detail = r.json()["detail"]
    assert r.status_code == 503 and "RuntimeError" in detail
    assert "spend guardrail" in detail and "GET /health" in detail


async def test_health_names_a_guardrail_block_without_probing(monkeypatch, mocker):
    """The 503 points operators at /health, so /health must show guardrail refusals too."""
    from types import SimpleNamespace

    from api.main import _check_llm_provider

    resolved = SimpleNamespace(
        provider=SimpleNamespace(name="openrouter", base_url="https://example.invalid", api_key_env=None),
        model="vendor/paid-model",
        tier=None,
    )
    monkeypatch.setattr("llm.client.resolve_agent_model", lambda agent_name: resolved)

    def blocked(agent_name, resolved):
        raise RuntimeError("paid model blocked: secret detail")

    monkeypatch.setattr("llm.client.check_spend_guardrail", blocked)
    sdk = mocker.patch("openai.OpenAI")
    result = await _check_llm_provider()
    assert result == {
        "status": "degraded",
        "detail": "openrouter:vendor/paid-model — blocked by spend guardrail: RuntimeError",
        "provider": "openrouter",
    }
    sdk.assert_not_called()


def test_a_missing_openrouter_key_is_refused_through_the_real_resolution(client, monkeypatch):
    """The deploy scenario unmocked: the flag on, DEFAULT_PROVIDER=openrouter and no key."""
    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "1")
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
    r = _upload(client)
    assert r.status_code == 503
    assert "ValueError" in r.json()["detail"]


def test_a_refused_upload_does_not_use_a_rate_limit_slot(client, monkeypatch):
    import api.main as api_main

    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "1")
    _no_provider(monkeypatch)
    api_main._upload_timestamps.clear()
    assert _upload(client).status_code == 503
    assert api_main._upload_timestamps == []


def test_the_pause_gate_answers_before_the_provider_gate(client, monkeypatch):
    monkeypatch.setenv("MAILROOM_UPLOAD_REQUIRE_PROVIDER", "1")
    _no_provider(monkeypatch)
    monkeypatch.setattr("pipeline.bins.is_ingestion_paused", lambda: True)
    r = _upload(client)
    assert r.status_code == 503 and "paused" in r.json()["detail"]


def test_producer_compose_forwards_the_free_only_guardrail():
    compose = (REPO_ROOT / "deploy" / "docker-compose.producer.yml").read_text(encoding="utf-8")
    assert 'MAILROOM_LLM_FREE_ONLY: "${MAILROOM_LLM_FREE_ONLY:-}"' in compose
