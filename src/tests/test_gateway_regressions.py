"""Gateway boundary tests; all provider calls are replaced at the SDK seam."""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIStatusError

from llm import client, providers, vision


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("DEFAULT_PROVIDER", "litellm")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://gateway.test:4000/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-gateway-key")
    monkeypatch.delenv("MAILROOM_GATEWAY_TIERS", raising=False)
    monkeypatch.setattr(providers, "_providers_cache", None)
    yield
    providers._providers_cache = None


@pytest.mark.parametrize("agent_tier,default_tier,expected", [
    ("vision", "fast", "vision"),
    (None, "extract", "extract"),
    (None, None, "api"),
])
def test_tier_fallback_precedence(monkeypatch, agent_tier, default_tier, expected):
    monkeypatch.delenv("MAILROOM_GATEWAY_TIERS", raising=False)
    monkeypatch.setattr(client, "load_config", lambda: {
        "gateway": {"default_tier": default_tier, "tiers": {
            "fast": {}, "extract": {}, "vision": {}, "api": {},
        }},
    })
    assert client.gateway_tier("example", {"tier": agent_tier}) == expected


def test_overrides_trim_ignore_incomplete_entries_and_refresh(gateway, monkeypatch):
    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS",
                       " ,sorter=vision,broken,=api,boss=, sorter = extract ")
    assert client.resolve_agent_model("sorter").model == "mailroom-extract"
    assert client.resolve_agent_model("boss").tier == "extract"
    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=api")
    resolved = client.resolve_agent_model("sorter")
    assert resolved.model == resolved.champion
    assert resolved.tier == "api"


@pytest.mark.parametrize("source", ["agent", "default", "override"])
def test_unknown_tier_never_constructs_client(gateway, monkeypatch, mocker, source):
    cfg = {"gateway": {"default_tier": "api", "tiers": {"api": {}}}}
    agent = {"model": "vendor/paid-model"}
    if source == "agent":
        agent["tier"] = "typo"
    elif source == "default":
        cfg["gateway"]["default_tier"] = "typo"
    else:
        monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=typo")
    monkeypatch.setattr(client, "load_config", lambda: cfg)
    monkeypatch.setattr(client, "get_agent_config", lambda name: agent)
    sdk = mocker.patch.object(client, "OpenAI")
    with pytest.raises(ValueError, match="unknown gateway tier 'typo'"):
        client.get_llm("sorter")
    sdk.assert_not_called()


@pytest.mark.parametrize("model", ["openrouter/free", "vendor/model:free", "vendor/paid-model"])
def test_api_tier_free_guard_precedes_sdk_creation(gateway, monkeypatch, mocker, model):
    monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
    monkeypatch.setattr(client, "get_agent_config", lambda name: {"tier": "api", "model": model})
    sdk = mocker.patch.object(client, "OpenAI")
    instrument = mocker.patch.object(client, "instrument_client", side_effect=lambda obj: obj)
    if model == "vendor/paid-model":
        with pytest.raises(RuntimeError, match="not free"):
            client.get_llm("sorter")
        sdk.assert_not_called()
        instrument.assert_not_called()
    else:
        actual_client, actual_model = client.get_llm("sorter")
        assert actual_model == model
        assert actual_client is sdk.return_value
        sdk.assert_called_once_with(base_url="http://gateway.test:4000/v1", api_key="test-gateway-key")
        instrument.assert_called_once_with(sdk.return_value)


@pytest.mark.parametrize("provider", ["vllm", "litellm"])
@pytest.mark.parametrize("effort,thinking", [("minimal", False), ("NONE", False), ("low", True), ("high", True)])
def test_thinking_flag_for_direct_and_gateway_vllm(gateway, monkeypatch, provider, effort, thinking):
    monkeypatch.setenv("DEFAULT_PROVIDER", provider)
    monkeypatch.setenv("VLLM_BASE_URL", "http://vllm.test/v1")
    assert client.reasoning_extra_body(effort, "sorter") == {
        "chat_template_kwargs": {"enable_thinking": thinking},
    }


def test_reasoning_resolution_failure_retains_openrouter_shape(mocker):
    mocker.patch.object(client, "resolve_agent_model", side_effect=ValueError("bad tier"))
    assert client.reasoning_extra_body("minimal", "sorter") == {"reasoning": {"effort": "minimal"}}
    assert client.reasoning_extra_body("", "sorter") is None


@pytest.mark.parametrize("method", ["_call_llm", "_call_structured"])
@pytest.mark.parametrize("tier,expected", [
    ("fast", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("api", {"reasoning": {"effort": "minimal"}}),
])
def test_native_calls_send_backend_reasoning(gateway, monkeypatch, mocker, method, tier, expected):
    from agents.base import BaseAgent

    class Agent(BaseAgent):
        agent_name = "sorter"

        def system_prompt(self):
            return "Return a json object."

    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", f"sorter={tier}")
    mocker.patch.object(client, "OpenAI")
    mocker.patch.object(client, "instrument_client", side_effect=lambda obj: obj)
    agent = Agent()
    mocker.patch.object(agent, "system_prompt_with_skills", return_value="Return json.")
    mocker.patch.object(agent, "_configured_reasoning_effort", return_value="minimal")
    completion = mocker.patch("agents.base.retry_chat_completion", return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))], usage=None,
    ))
    if method == "_call_structured":
        assert agent._call_structured("Document", {"type": "object"}) == {"ok": True}
    else:
        assert agent._call_llm("Document") == '{"ok": true}'
    assert completion.call_args.kwargs["extra_body"] == expected
    assert completion.call_args.kwargs["model"] == client.resolve_agent_model("sorter").model


@pytest.fixture
def langchain_agent():
    from langchain_agents.base_agent import BaseAgent

    class Agent(BaseAgent):
        agent_name = "sorter"

        def system_prompt(self):
            return "Return json."

    return Agent(api_key="test-gateway-key")


@pytest.mark.no_langchain_mock
def test_langchain_resolves_alias_accounts_model_and_caches_client(gateway, langchain_agent, mocker):
    sdk = mocker.patch("langchain_agents.base_agent.ChatOpenAI")
    langchain_agent._reasoning_effort = "minimal"
    assert langchain_agent.llm() is sdk.return_value
    assert langchain_agent.llm() is sdk.return_value
    sdk.assert_called_once()
    assert sdk.call_args.kwargs["model"] == langchain_agent.model == "mailroom-fast"
    assert sdk.call_args.kwargs["base_url"] == "http://gateway.test:4000/v1"
    assert sdk.call_args.kwargs["max_retries"] == 0
    assert sdk.return_value.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.no_langchain_mock
def test_langchain_guards_paid_api_route(gateway, langchain_agent, monkeypatch, mocker):
    monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
    monkeypatch.setattr(client, "get_agent_config", lambda name: {"tier": "api", "model": "vendor/paid-model"})
    sdk = mocker.patch("langchain_agents.base_agent.ChatOpenAI")
    with pytest.raises(RuntimeError, match="not free"):
        langchain_agent.llm()
    sdk.assert_not_called()


def test_langchain_per_call_reasoning_override(gateway, langchain_agent, mocker):
    llm = Mock()
    llm.bind.return_value.invoke.return_value = SimpleNamespace(
        content="ok", usage_metadata={}, response_metadata={},
    )
    mocker.patch.object(langchain_agent, "llm", return_value=llm)
    assert langchain_agent._call_llm("Document", system_prompt="Read.", reasoning_effort="minimal") == "ok"
    llm.bind.assert_called_once_with(extra_body={"chat_template_kwargs": {"enable_thinking": False}})


@pytest.mark.parametrize("tier,expected", [("fast", False), ("extract", False), ("vision", True)])
def test_vision_uses_wire_alias_before_client_construction(gateway, monkeypatch, langchain_agent, tier, expected):
    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", f"sorter={tier}")
    assert vision.agent_uses_vision("sorter") is expected
    assert langchain_agent._uses_vision() is expected
    assert langchain_agent._llm is None


def test_pipeline_vision_tracks_specialist_override(gateway, monkeypatch):
    assert vision.pipeline_uses_vision() is False
    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "contracts_specialist=vision")
    assert vision.pipeline_uses_vision() is True


def test_vision_exclusion_wins_over_case_insensitive_allowlist(monkeypatch):
    monkeypatch.setattr(vision, "_vision_config", lambda: {"enabled": True, "models": ["MAILROOM"], "exclude": ["FAST"]})
    assert vision.is_vision_capable("mailroom-vision")
    assert not vision.is_vision_capable("Mailroom-Fast")
    assert not vision.is_vision_capable("")


@pytest.mark.parametrize("attempt,expected", [(0, 90), (1, 90), (2, 180), (3, 240), (9, 240)])
def test_gateway_cold_start_backoff_is_bounded(gateway, attempt, expected):
    from llm.retry import retry_sleep_seconds

    exc = APIStatusError("cold", response=httpx.Response(503, request=httpx.Request("GET", "http://gateway.test")), body=None)
    cfg = {"jitter": 0, "modal_cold_start_delay": 90, "modal_cold_start_max_delay": 240}
    assert retry_sleep_seconds(exc, attempt, cfg, base_url="http://gateway.test:4000/v1/") == expected


@pytest.mark.parametrize("status,url,configured,expected", [
    (500, "http://gateway.test:4000/v1", True, 2),
    (429, "http://gateway.test:4000/v1", True, 16),
    (503, "https://other.test/v1", True, 2),
    (503, "http://gateway.test:4000/v1", False, 2),
    (503, None, True, 2),
])
def test_other_errors_keep_standard_retry_ladder(gateway, monkeypatch, status, url, configured, expected):
    from llm.retry import retry_sleep_seconds

    if not configured:
        monkeypatch.delenv("LITELLM_BASE_URL")
    exc = APIStatusError("error", response=httpx.Response(status, request=httpx.Request("GET", "http://gateway.test")), body=None)
    assert retry_sleep_seconds(exc, 2, {"base_delay": 1, "rate_limit_base_delay": 8, "jitter": 0}, base_url=url) == expected


def test_langchain_retry_passes_gateway_endpoint(gateway, langchain_agent, mocker):
    langchain_agent._llm = SimpleNamespace(openai_api_base="http://gateway.test:4000/v1")
    exc = APIStatusError("cold", response=httpx.Response(503, request=httpx.Request("GET", "http://gateway.test")), body=None)
    invoke = Mock(side_effect=[exc, "ok"])
    sleep = mocker.patch("time.sleep")
    mocker.patch("pipeline.config.load_config", return_value={"llm_retry": {
        "max_attempts": 2, "jitter": 0, "modal_cold_start_delay": 90,
    }})
    assert langchain_agent._invoke_with_retry(invoke) == "ok"
    assert invoke.call_count == 2
    sleep.assert_called_once_with(90)


async def test_health_reports_resolved_gateway_tier(gateway, mocker):
    from api.main import _check_llm_provider

    sdk = mocker.patch("openai.OpenAI")
    assert await _check_llm_provider() == {
        "status": "ok", "detail": "litellm:mailroom-fast[fast]", "provider": "litellm",
    }
    sdk.return_value.models.list.assert_called_once_with()
    assert sdk.call_args.kwargs["api_key"] == "test-gateway-key"
    assert sdk.call_args.kwargs["max_retries"] == 0
    sdk.return_value.chat.completions.create.assert_not_called()


async def test_health_reports_bad_tier_without_contacting_provider(gateway, monkeypatch, mocker):
    from api.main import _check_llm_provider

    monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=typo")
    sdk = mocker.patch("openai.OpenAI")
    result = await _check_llm_provider()
    assert result["status"] == "degraded"
    assert result["provider"] is None
    assert "unknown gateway tier 'typo'" in result["detail"]
    sdk.assert_not_called()
