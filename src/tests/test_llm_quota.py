"""Free-model quota breaker + OpenRouter `models` fallback (llm/quota.py, llm/retry.py)."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import RateLimitError

from llm.quota import FreeQuotaBreaker, FreeQuotaExhausted


def _rl(retry_after=None) -> RateLimitError:
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return RateLimitError("rate limited", response=resp, body={"message": "rate limited"})


def test_breaker_opens_after_three_429s_and_closes_after_cooldown():
    b = FreeQuotaBreaker(trip_after=3, cooldown_s=300)
    b.note_rate_limit(now=0)
    b.note_rate_limit(now=1)
    assert b.is_open(now=2) is False
    b.note_rate_limit(retry_after_s=60, now=2)
    assert b.is_open(now=3) is True
    assert b.is_open(now=61) is True
    assert b.is_open(now=63) is False


def test_breaker_uses_default_cooldown_without_retry_after():
    b = FreeQuotaBreaker(trip_after=3, cooldown_s=300)
    for t in range(3):
        b.note_rate_limit(now=t)
    assert b.open_until == pytest.approx(2 + 300)
    # A second consecutive trip doubles the cooldown, capped at 3600.
    for t in range(3):
        b.note_rate_limit(now=400 + t)
    assert b.open_until == pytest.approx(402 + 600)


def test_cooldown_capped_at_one_hour():
    b = FreeQuotaBreaker(trip_after=1, cooldown_s=3000)
    b.note_rate_limit(now=0)
    b.note_rate_limit(now=4000)
    assert b.open_until == pytest.approx(4000 + 3600)


def test_success_resets_consecutive_count():
    b = FreeQuotaBreaker(trip_after=3, cooldown_s=300)
    b.note_rate_limit(now=0)
    b.note_rate_limit(now=1)
    b.note_success()
    b.note_rate_limit(now=2)
    b.note_rate_limit(now=3)
    assert b.is_open(now=4) is False


class _Client:
    def __init__(self, base_url="https://openrouter.ai/api/v1", fail=0, model_served="x/y:free"):
        self.base_url = base_url
        self.kwargs = []
        self.fail = fail
        self.model_served = model_served
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.kwargs.append(kwargs)
                if outer.fail:
                    outer.fail -= 1
                    raise _rl()
                return SimpleNamespace(model=outer.model_served, usage=None,
                                       choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def test_openrouter_free_call_sends_models_array(monkeypatch, no_sleep):
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: ["a:free", "b:free"])
    client = _Client()
    retry.retry_chat_completion(
        client, model="a:free", messages=[], extra_body={"reasoning": {"effort": "low"}}
    )
    body = client.kwargs[0]["extra_body"]
    assert client.kwargs[0]["model"] == "a:free"
    assert body["models"] == ["b:free"]  # fallbacks only, never the primary
    assert body["provider"]["require_parameters"] is True
    assert body["reasoning"] == {"effort": "low"}


def test_single_entry_swarm_sends_no_models_array(monkeypatch, no_sleep):
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: ["a:free"])
    client = _Client()
    retry.retry_chat_completion(client, model="a:free", messages=[])
    assert "extra_body" not in client.kwargs[0]


def test_non_openrouter_url_keeps_client_rotation(monkeypatch, no_sleep):
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: ["a:free", "b:free"])
    client = _Client(base_url="http://localhost:8080/v1", fail=1)
    retry.retry_chat_completion(client, model="a:free", messages=[])
    assert [k["model"] for k in client.kwargs] == ["a:free", "b:free"]
    assert "extra_body" not in client.kwargs[0]


def test_paid_model_gets_no_models_array_and_ignores_breaker(monkeypatch, no_sleep):
    import llm.quota as quota
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: ["a:free", "b:free"])
    breaker = quota.get_breaker()
    breaker.open_until = time.time() + 1000
    client = _Client(fail=2)
    retry.retry_chat_completion(client, model="acme/paid", messages=[], max_attempts=5)
    assert len(client.kwargs) == 3
    assert "extra_body" not in client.kwargs[0]
    assert breaker.is_open() is True  # paid calls never close it either


def test_open_breaker_short_circuits_free_call(monkeypatch, no_sleep):
    import llm.quota as quota
    import llm.retry as retry

    quota.get_breaker().open_until = time.time() + 1000
    client = _Client()
    with pytest.raises(FreeQuotaExhausted):
        retry.retry_chat_completion(client, model="a:free", messages=[])
    assert client.kwargs == []


def test_free_success_closes_breaker_count(monkeypatch, no_sleep):
    import llm.quota as quota
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: [])
    client = _Client(fail=2)
    retry.retry_chat_completion(client, model="a:free", messages=[], max_attempts=5)
    assert quota.get_breaker().is_open() is False
    assert quota.get_breaker()._consecutive == 0


def test_breaker_opening_on_final_attempt_raises_quota_exhausted(monkeypatch, no_sleep):
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: [])
    client = _Client(fail=5)
    with pytest.raises(FreeQuotaExhausted) as info:
        retry.retry_chat_completion(client, model="a:free", messages=[], max_attempts=3)
    assert len(client.kwargs) == 3  # the third 429 opened it on the last attempt
    assert info.value.__cause__ is not None
