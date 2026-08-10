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
