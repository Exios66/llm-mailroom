"""#260: a 200 response with null/empty ``choices`` must be a transient provider
error, not a TypeError that parks the document as ``judge_error``.

OpenRouter answers HTTP 200 with ``choices: null`` and an ``error`` body when the
upstream provider fails mid-generation. ``retry_chat_completion`` used to return
that response untouched and ``BaseAgent._call_llm`` indexed ``choices[0]``.
"""

import time
from types import SimpleNamespace

import pytest


def _null_choices_response():
    return SimpleNamespace(
        choices=None,
        usage=None,
        model="qwen/qwen3.7-flash",
        error={"code": 502, "message": "upstream provider error"},
    )


class _Completions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        return item


def _client(responses):
    comps = _Completions(responses)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=comps), base_url="https://openrouter.ai/api/v1"
    )
    return client, comps


def _ok_response(content="{}"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
        model="qwen/qwen3.7-flash",
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


class TestRetryChatCompletionEmptyChoices:
    def test_retries_then_raises_typed_error(self):
        from llm.retry import EmptyChoicesError, retry_chat_completion

        client, comps = _client([_null_choices_response()])
        with pytest.raises(EmptyChoicesError) as info:
            retry_chat_completion(client, model="qwen/qwen3.7-flash", messages=[], max_attempts=3)
        assert comps.calls == 3
        text = str(info.value)
        assert "502" in text and "upstream provider error" in text

    def test_recovers_when_a_later_attempt_has_choices(self):
        from llm.retry import retry_chat_completion

        ok = _ok_response()
        client, comps = _client([_null_choices_response(), ok])
        result = retry_chat_completion(client, model="qwen/qwen3.7-flash", messages=[], max_attempts=3)
        assert result is ok
        assert comps.calls == 2

    @pytest.mark.parametrize(
        "bad",
        [
            SimpleNamespace(choices=[], usage=None, model="m"),
            SimpleNamespace(choices=[SimpleNamespace(message=None)], usage=None, model="m"),
        ],
    )
    def test_empty_list_and_missing_message_are_errors(self, bad):
        from llm.retry import EmptyChoicesError, retry_chat_completion

        client, comps = _client([bad])
        with pytest.raises(EmptyChoicesError):
            retry_chat_completion(client, model="m", messages=[], max_attempts=2)
        assert comps.calls == 2

    def test_is_transient_error(self):
        from llm.retry import EmptyChoicesError, is_transient_error

        assert is_transient_error(EmptyChoicesError("choices is null (502: upstream provider error)"))


    @pytest.mark.parametrize("code", [400, 401, 402, 403, 404, 422])
    def test_client_error_code_in_body_fails_fast(self, code):
        from llm.retry import EmptyChoicesError, is_transient_error, retry_chat_completion

        resp = SimpleNamespace(choices=None, usage=None, model="m", error={"code": code, "message": "nope"})
        client, comps = _client([resp])
        with pytest.raises(EmptyChoicesError) as info:
            retry_chat_completion(client, model="m", messages=[], max_attempts=3)
        assert comps.calls == 1
        assert info.value.status_code == code
        assert not is_transient_error(info.value)

    @pytest.mark.parametrize("code", [None, 408, 429, 500, 502, 503, 504, 529])
    def test_transient_or_missing_code_is_retryable(self, code):
        from llm.retry import EmptyChoicesError, is_transient_error

        assert is_transient_error(EmptyChoicesError("choices is null", status_code=code))


class TestJudgeNodeTransientOnEmptyChoices:
    def _judge(self, responses):
        from agents.judge import CompletenessJudge

        client, comps = _client(responses)
        j = CompletenessJudge.__new__(CompletenessJudge)
        j.agent_name, j.model, j._langfuse_prompt = "judge", "qwen/qwen3.7-flash", None
        j.client = client
        j.system_prompt = lambda: "sys"
        return j, comps

    def test_judge_completeness_raises_transient_not_typeerror(self):
        from llm.retry import is_transient_error

        j, _ = self._judge([_null_choices_response()])
        with pytest.raises(Exception) as info:
            j.judge_completeness(doc_type="contract", extracted={"document_name": "x"}, doc_text="text")
        assert not isinstance(info.value, TypeError)
        assert is_transient_error(info.value)

    def test_judge_verify_node_returns_transient_update(self, monkeypatch):
        import agents.judge as jmod
        from graph import build_graph as bg
        from graph.routing import after_judge

        j, _ = self._judge([_null_choices_response()])
        monkeypatch.setattr(jmod, "CompletenessJudge", lambda: j)
        monkeypatch.setattr(bg, "judge_should_run", lambda state: True)
        state = {
            "doc_id": "d1",
            "doc_type": "contract",
            "doc_text": "text",
            "extraction_confidence": 0.5,
            "extracted_data": {"document_name": "x"},
        }
        updates = bg.judge_verify_node(state)
        assert updates.get("judge_verdict") != "judge_error"
        assert updates["transient_error"] is True
        assert updates["transient_retries_judge_verify"] == 1
        assert "transient provider error" in updates["error_message"]
        assert "upstream provider error" in updates["error_message"]
        merged = {**state, **updates}
        assert after_judge(merged) == "judge_verify"


class TestHardFailEscalationCarriesMessage:
    def test_judge_hard_fail_reason_includes_exception_message(self, monkeypatch):
        import agents.judge as jmod
        from graph import build_graph as bg

        class BoomJudge:
            def __init__(self):
                raise RuntimeError("judge exploded with a very specific cause")

        monkeypatch.setattr(jmod, "CompletenessJudge", BoomJudge)
        monkeypatch.setattr(bg, "judge_should_run", lambda state: True)
        updates = bg.judge_verify_node({"doc_id": "d1", "doc_type": "contract", "doc_text": "t"})
        assert updates["judge_verdict"] == "judge_error"
        reason = updates["escalation_reason"]
        assert reason.startswith("judge verification failed (RuntimeError)")
        assert "judge exploded with a very specific cause" in reason

    def test_message_is_truncated(self, monkeypatch):
        import agents.judge as jmod
        from graph import build_graph as bg

        class BoomJudge:
            def __init__(self):
                raise RuntimeError("x" * 2000)

        monkeypatch.setattr(jmod, "CompletenessJudge", BoomJudge)
        monkeypatch.setattr(bg, "judge_should_run", lambda state: True)
        updates = bg.judge_verify_node({"doc_id": "d1", "doc_type": "contract", "doc_text": "t"})
        reason = updates["escalation_reason"]
        assert "x" * 300 in reason
        assert "x" * 301 not in reason


class TestCauseNeverReachesTheSender:
    """Round-2 review: the ``[cause: …]`` audit suffix must not be echoed to the
    external sender (exception text can carry paths, inputs, provider detail)."""

    SECRET = "input_value='/home/svc/secret/key.txt' provider=OpenRouter sk-abc123"

    def _manifest(self, stage):
        return {
            "doc_id": "c-1", "matter_id": "M", "original_filename": "c.txt", "stage": stage,
            "escalation_reason": (
                "classification failed (ValidationError) -- routing to human review "
                f"[cause: 1 validation error ] {self.SECRET}]"
            ),
            "intake": {"source": "gmail", "message_id": "m", "sender": "a@example.com"},
        }

    @pytest.mark.parametrize("stage", ["review", "failed"])
    def test_echo_text_and_html_drop_the_cause(self, stage):
        from pipeline.gmail_intake import _digest_summary, build_echo_body, build_echo_html

        manifest = self._manifest(stage)
        body = build_echo_body(manifest, [], True)
        html = build_echo_html(manifest, [], True)
        digest = _digest_summary(manifest)
        for rendered in (body, html, str(digest)):
            assert "secret" not in rendered and "sk-abc123" not in rendered
            assert "[cause:" not in rendered
        assert "classification failed (ValidationError)" in body
        assert digest["reason"].startswith("classification failed (ValidationError)")
        # the manifest itself keeps the full audit text
        assert self.SECRET in manifest["escalation_reason"]

    def test_reason_without_a_cause_is_unchanged(self):
        from pipeline.gmail_intake import _sender_reason

        assert _sender_reason({"escalation_reason": "low confidence"}) == "low confidence"
        assert _sender_reason({"error_message": "boom"}) == "boom"
        assert _sender_reason({}) == ""
