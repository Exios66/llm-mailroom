import pytest


class TestStructuredCallJsonInvariant:
    """The `json_object` response format requires the literal token `json` in
    the messages for some providers (Qwen via Alibaba rejects with HTTP 400
    otherwise). Every `_call_structured` request on the mailroom's own base
    (agents/base.py) must carry it regardless of the document content.

    NOTE: the LangChain sorter/contracts specialist no longer use this base —
    they use with_structured_output (JSON-schema method) via the vendored
    langchain_agents stack and are exempt from this invariant.
    """

    def test_messages_contain_literal_json_token(self, sample_corporate_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"entity_name": "ACME", "record_type": "bylaws", "confidence": 0.9}'
        )
        from agents.corporate_records_specialist import CorporateRecordsSpecialist
        agent = CorporateRecordsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        agent.extract(sample_corporate_text[:1000])

        kwargs = mock_openai_client.chat.completions.create.call_args.kwargs
        messages = kwargs["messages"]
        assert messages, "expected messages in call kwargs"

        user_content = messages[-1]["content"]
        assert "json" in user_content.lower(), (
            "user message must contain the literal token 'json' so providers "
            "accept response_format json_object"
        )

    def test_response_format_is_json_object(self, sample_corporate_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"entity_name": "ACME", "record_type": "bylaws", "confidence": 0.9}'
        )
        from agents.corporate_records_specialist import CorporateRecordsSpecialist
        agent = CorporateRecordsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        agent.extract(sample_corporate_text[:1000])

        kwargs = mock_openai_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("response_format") == {"type": "json_object"}

    def test_invariant_holds_even_when_doc_has_no_json_word(self, mock_openai_client):
        text = "This document contains absolutely no structured-data keywords at all."
        assert "json" not in text.lower()
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"entity_name": "ACME", "record_type": "bylaws", "confidence": 0.5}'
        )
        from agents.corporate_records_specialist import CorporateRecordsSpecialist
        agent = CorporateRecordsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        agent.extract(text)

        kwargs = mock_openai_client.chat.completions.create.call_args.kwargs
        messages = kwargs["messages"]
        assert "json" in messages[-1]["content"].lower()


class TestVendoredRetryContract:
    """L-16/L-17: vendored agents route invokes through the shared retry
    contract — SDK max_retries=0, retryable-only filtering, deadline checks."""

    def test_sdk_max_retries_disabled(self):
        # conftest mocks ChatOpenAI + BaseAgent.llm, so assert the CONTRACT at
        # the source file: the ChatOpenAI(...) constructor uses max_retries=0
        # (single retry layer — L-16/L-17).
        import inspect
        import langchain_agents.base_agent as ba

        src = inspect.getsource(ba)
        assert "max_retries=0" in src
        # The constructor call itself must not enable SDK retries.
        call = src[src.index("ChatOpenAI("):src.index("if self._reasoning_effort:")]
        assert "max_retries=0" in call

    def test_retryable_error_is_retried(self, monkeypatch):
        from langchain_agents.base_agent import BaseAgent

        class A(BaseAgent):
            agent_name = "t"

            def system_prompt(self) -> str:
                return "p"

        agent = A(model="qwen/qwen3.7-flash")
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                import openai

                raise openai.APIConnectionError(request=None)
            return "ok"

        monkeypatch.setattr(agent, "_check_deadline", lambda: None)
        result = agent._invoke_with_retry(flaky)
        assert result == "ok"
        assert calls["n"] == 3

    def test_non_retryable_error_not_retried(self, monkeypatch):
        from langchain_agents.base_agent import BaseAgent

        class A(BaseAgent):
            agent_name = "t"

            def system_prompt(self) -> str:
                return "p"

        agent = A(model="qwen/qwen3.7-flash")
        calls = {"n": 0}

        import openai
        import httpx

        def bad_request():
            calls["n"] += 1
            resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
            raise openai.BadRequestError("400 json marker", response=resp, body=None)

        monkeypatch.setattr(agent, "_check_deadline", lambda: None)
        with pytest.raises(openai.BadRequestError):
            agent._invoke_with_retry(bad_request)
        assert calls["n"] == 1  # never retried

    def test_exhausted_attempts_raise(self, monkeypatch):
        from langchain_agents.base_agent import BaseAgent

        class A(BaseAgent):
            agent_name = "t"

            def system_prompt(self) -> str:
                return "p"

        agent = A(model="qwen/qwen3.7-flash")
        calls = {"n": 0}

        import openai

        def always_fail():
            calls["n"] += 1
            raise openai.APIConnectionError(request=None)

        monkeypatch.setattr(agent, "_check_deadline", lambda: None)
        with pytest.raises(openai.APIConnectionError):
            agent._invoke_with_retry(always_fail)
        assert calls["n"] == 5  # max_attempts from taxonomy llm_retry
