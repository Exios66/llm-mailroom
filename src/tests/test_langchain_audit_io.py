"""LangChain calls supply the same local audit evidence as native agent calls."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from agents.contracts_specialist import ContractsSpecialist
from audit.trace import llm_info_since
from langchain_agents.base_agent import BaseAgent as LangChainBaseAgent
from langchain_agents.mock import FakeLangChainLLM
from llm.retry import EmptyChoicesError
from llm.prompt_sets import resolve
from pipeline.limits import reset_run_usage, run_calls, usage_summary


def test_structured_call_records_full_evidence_and_repo_prompt_identity(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    reset_run_usage()
    fake = FakeLangChainLLM()
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        result = agent.extract("SYNTHETIC AGREEMENT BETWEEN ALPHA AND BETA")

    assert not result.get("_parse_error")
    assert fake.calls == 1
    assert usage_summary()["calls"] == 1
    assert usage_summary()["prompt_tokens"] == 120
    call = run_calls()[0]
    prompt = json.loads(call["prompt"])
    assert prompt[0]["role"] == "system"
    assert prompt[1]["role"] == "user"
    assert "SYNTHETIC AGREEMENT BETWEEN ALPHA AND BETA" in prompt[1]["content"]
    assert json.loads(call["completion"])["document_name"] == "Mock Agreement"
    assert call["prompt_version"] == resolve(agent.agent_name)[0]
    info = llm_info_since(0)
    assert info["prompt"] == call["prompt"]
    assert info["completion"] == call["completion"]


def test_plain_and_vision_calls_redact_page_data_without_losing_text():
    reset_run_usage()
    fake = FakeLangChainLLM()
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        agent._uses_vision = lambda: True
        agent._call_llm("FULL TEXT", pages=["data:image/png;base64,SECRET-PAGE-DATA"])
        agent._call_structured(
            "STRUCTURED TEXT", {"type": "object"},
            pages=["data:image/png;base64,SECRET-STRUCTURED-PAGE"],
        )
        agent._call_vision_multi("SYSTEM", "VISIBLE INSTRUCTION", [("SECRET-SECOND-PAGE", "png")])

    assert fake.calls == 3
    assert usage_summary()["calls"] == 3
    assert run_calls()[-1]["prompt_version"] is None
    for call, expected_text, secret in zip(
        run_calls(), ("FULL TEXT", "STRUCTURED TEXT", "VISIBLE INSTRUCTION"),
        ("SECRET-PAGE-DATA", "SECRET-STRUCTURED-PAGE", "SECRET-SECOND-PAGE"),
    ):
        assert expected_text in call["prompt"]
        assert secret not in call["prompt"]
        assert json.loads(call["prompt"])[1]["content"][1] == {
            "type": "image_url", "omitted": True,
        }
        assert call["completion"] is not None


def test_chunked_extraction_records_each_window_once():
    reset_run_usage()
    fake = FakeLangChainLLM()
    document = "FIRST CLAUSE " * 8 + "\n\n" + "LAST CLAUSE " * 8
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        result = agent.extract_chunked(document, chunk_chars=100, overlap_chars=0)

    assert not result.get("_parse_error")
    assert fake.calls > 1
    assert usage_summary()["calls"] == fake.calls
    assert usage_summary()["prompt_tokens"] == fake.calls * 120
    prompts = "\n".join(call["prompt"] for call in run_calls())
    assert "FIRST CLAUSE" in prompts
    assert "LAST CLAUSE" in prompts


def test_structured_tool_call_records_raw_arguments_and_served_model():
    reset_run_usage()
    raw = SimpleNamespace(
        content="", usage_metadata={"input_tokens": 3, "output_tokens": 4},
        response_metadata={"model_name": "served-model"},
        tool_calls=[{"name": "extract", "args": {"name": "Alpha"}}],
        additional_kwargs={},
    )
    runner = SimpleNamespace(invoke=lambda messages, **kwargs: {
        "raw": raw, "parsed": {"name": "Alpha"}, "parsing_error": None,
    })
    fake = FakeLangChainLLM()
    fake.with_structured_output = lambda *args, **kwargs: runner
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        result = agent._call_structured("FULL REQUEST", {"type": "object"})

    assert result == {"name": "Alpha"}
    assert usage_summary()["calls"] == 1
    assert run_calls()[0]["served_model"] == "served-model"
    assert json.loads(run_calls()[0]["completion"])[0]["args"]["name"] == "Alpha"


def test_retry_records_only_the_successful_call(monkeypatch):
    reset_run_usage()
    fake = FakeLangChainLLM()
    original = fake.invoke
    attempts = 0

    def flaky(messages, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise EmptyChoicesError("temporary upstream failure", status_code=503)
        return original(messages, **kwargs)

    fake.invoke = flaky
    monkeypatch.setattr("llm.retry.retry_sleep_seconds", lambda *args, **kwargs: 0)
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        agent._call_llm("RETRY REQUEST")

    assert attempts == 2
    assert usage_summary()["calls"] == 1
    assert "RETRY REQUEST" in run_calls()[0]["prompt"]


def test_explicit_prompt_override_has_no_stale_repo_or_managed_version():
    reset_run_usage()
    fake = FakeLangChainLLM()
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        agent._langfuse_prompt = SimpleNamespace(version=17)
        agent._call_llm("REQUEST", system_prompt="EXPLICIT SYSTEM PROMPT")
        agent._call_structured("REQUEST", {"type": "object"}, system_prompt="EXPLICIT STRUCTURED PROMPT")

    assert [call["prompt_version"] for call in run_calls()] == [None, None]
    assert "EXPLICIT SYSTEM PROMPT" in run_calls()[0]["prompt"]
    assert "EXPLICIT STRUCTURED PROMPT" in run_calls()[1]["prompt"]


def test_list_shaped_completion_preserves_text_and_redacts_images():
    reset_run_usage()
    fake = FakeLangChainLLM()
    original = fake._make_message

    def list_response(parsed):
        message = original(parsed)
        message.content = [
            {"type": "text", "text": "VISIBLE RESPONSE"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET-OUTPUT"}},
        ]
        return message

    fake._make_message = list_response
    with patch.object(LangChainBaseAgent, "llm", lambda self: fake):
        agent = ContractsSpecialist()
        agent._call_llm("REQUEST")

    completion = run_calls()[0]["completion"]
    assert "VISIBLE RESPONSE" in completion
    assert "SECRET-OUTPUT" not in completion
    assert json.loads(completion)[1] == {"type": "image_url", "omitted": True}
