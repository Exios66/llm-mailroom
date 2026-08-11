import time
from unittest.mock import MagicMock

import pytest
from openai import APIConnectionError, APITimeoutError, BadRequestError, RateLimitError


def _http_response(status: int):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.http_version = "1.1"
    return resp


class TestRetryChatCompletion:
    def test_retries_connection_error(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        from llm.retry import retry_chat_completion

        client = MagicMock()
        ok = object()
        client.chat.completions.create.side_effect = [APIConnectionError(request=object()), ok]
        result = retry_chat_completion(client, model="m", messages=[], max_attempts=3)
        assert result is ok
        assert client.chat.completions.create.call_count == 2

    def test_retries_timeout_and_rate_limit(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        from llm.retry import retry_chat_completion

        client = MagicMock()
        ok = object()
        client.chat.completions.create.side_effect = [
            APITimeoutError(request=object()),
            RateLimitError(
                "rate limited",
                response=_http_response(429),
                body={"message": "rate limited"},
            ),
            ok,
        ]
        result = retry_chat_completion(client, model="m", messages=[], max_attempts=5)
        assert result is ok
        assert client.chat.completions.create.call_count == 3

    def test_does_not_retry_generic_4xx(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        from llm.retry import retry_chat_completion

        client = MagicMock()
        err = BadRequestError(
            "some other bad request",
            response=_http_response(400),
            body={"message": "some other bad request"},
        )
        client.chat.completions.create.side_effect = err
        with pytest.raises(BadRequestError):
            retry_chat_completion(client, model="m", messages=[], max_attempts=3)
        assert client.chat.completions.create.call_count == 1

    def test_retries_qwen_json_mode_400(self, monkeypatch):
        # Alibaba/Qwen intermittently rejects the json_object request with a
        # 400 "must contain the word 'json'" even though the exact same
        # messages succeed on other OpenRouter routes; this specific 400 is a
        # documented retryable exception (see llm/retry.py).
        monkeypatch.setattr(time, "sleep", lambda s: None)
        from llm.retry import retry_chat_completion

        client = MagicMock()
        ok = object()
        err = BadRequestError(
            "'messages' must contain the word 'json'",
            response=_http_response(400),
            body={"message": "'messages' must contain the word 'json'"},
        )
        client.chat.completions.create.side_effect = [err, ok]
        result = retry_chat_completion(client, model="m", messages=[], max_attempts=3)
        assert result is ok
        assert client.chat.completions.create.call_count == 2

    def test_exhausts_attempts_then_raises(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda s: None)
        from llm.retry import retry_chat_completion

        client = MagicMock()
        client.chat.completions.create.side_effect = APIConnectionError(request=object())
        with pytest.raises(APIConnectionError):
            retry_chat_completion(client, model="m", messages=[], max_attempts=3)
        assert client.chat.completions.create.call_count == 3
