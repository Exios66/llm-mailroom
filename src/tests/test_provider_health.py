"""Passive per-provider LLM health (pager input): every chat-completion
attempt through llm/retry.py is recorded; nothing ever probes a provider."""
import json
import time
from unittest.mock import MagicMock

import pytest
from openai import APIStatusError


@pytest.fixture
def ph(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_PROVIDER_HEALTH", "1")
    from llm import provider_health

    monkeypatch.setattr(provider_health, "_BUF", provider_health._Buffer())
    return provider_health


def test_provider_key_mapping(monkeypatch):
    from llm.provider_health import provider_key

    monkeypatch.setenv("LITELLM_BASE_URL", "https://gw.example.com/v1")
    assert provider_key("https://acme--mailroom-vllm-extract.modal.run/v1", "x") == "modal:extract"
    assert provider_key("https://gw.example.com/v1", "mailroom-vision") == "modal:vision"
    assert provider_key("https://openrouter.ai/api/v1", "qwen/q") == "openrouter"
    assert provider_key("http://localhost:8000/v1", "m") == "other:localhost"
    assert provider_key(None, None) == "other:unknown"


def test_window_counts_and_consecutive(ph):
    url, t = "https://acme--mailroom-vllm-extract.modal.run/v1", 1_000_000.0
    for i, (ok, st) in enumerate([(True, 200), (False, 503), (False, 502), (True, 200), (False, 503), (False, 503)]):
        ph.record_llm_outcome(url, "m", ok=ok, status=st, latency_s=1.0 + i, now=t + i)
    h = ph.read_provider_health(now=t + 10)[ph.provider_key(url, "m")]
    # a Modal 503 is a cold start, not an outage: only the 502 counts as an error
    assert h["calls"] == 6 and h["errors"] == 1 and h["consecutive_errors"] == 0 and h["ok_calls"] == 2
    assert h["count_503"] == 3 and h["last_status"] == 503 and h["last_ok_at"] == t + 3
    assert h["p50_latency_s"] == 3.5


def test_openrouter_503_is_an_outage(ph):
    """Only Modal's 503 means cold start; OpenRouter answering 503 is an outage."""
    url = "https://openrouter.ai/api/v1"
    for i in range(3):
        ph.record_llm_outcome(url, "m", ok=False, status=503, latency_s=0.1, now=1000.0 + i)
    h = ph.read_provider_health(now=1005.0)["openrouter"]
    assert h["errors"] == 3
    assert h["consecutive_errors"] == 3
    assert h["last_status"] == 503


def test_old_events_age_out(ph):
    url, t = "https://openrouter.ai/api/v1", 1_000_000.0
    ph.record_llm_outcome(url, "m", ok=False, status=503, latency_s=1.0, now=t)
    ph.record_llm_outcome(url, "m", ok=True, status=200, latency_s=1.0, now=t + 700)
    h = ph.read_provider_health(now=t + 700)["openrouter"]
    assert h["calls"] == 1 and h["errors"] == 0 and h["count_503"] == 0
    assert ph.read_provider_health(now=t + 5000) == {}


def test_corrupt_file_ignored_then_rewritten(ph):
    path = ph._path()
    path.write_text("{not json")
    assert ph.read_provider_health(now=1.0) == {}
    ph.record_llm_outcome("https://openrouter.ai/api/v1", "m", ok=False, status=500, latency_s=0.5, now=2.0)
    ph._BUF.flush()
    assert json.loads(path.read_text())["openrouter"][0][1] is False


def test_two_writers_merge(ph):
    a, b = ph._Buffer(), ph._Buffer()
    a.add("modal:extract", [10.0, True, 200, 1.0], now=10.0)
    b.add("modal:extract", [11.0, False, 503, 2.0], now=11.0)
    b.add("openrouter", [12.0, True, 200, 0.4], now=12.0)
    a.flush()
    b.flush()
    data = json.loads(ph._path().read_text())
    assert len(data["modal:extract"]) == 2 and len(data["openrouter"]) == 1
    assert ph.read_provider_health(now=20.0)["modal:extract"]["calls"] == 2


@pytest.mark.parametrize("failure_point", ["path", "lock", "serialize", "write", "replace"])
def test_failed_flush_restores_pending_and_new_events(ph, monkeypatch, failure_point):
    first = [1.0, True, 200, 0.1]
    second = [2.0, True, 200, 0.2]
    same_provider = [3.0, True, 200, 0.3]
    new_provider = [4.0, True, 200, 0.4]
    ph._BUF.add("openrouter", first, now=1.0)
    ph._BUF.add("modal:vllm", second, now=2.0)
    error = TypeError("flush failed") if failure_point == "serialize" else OSError("flush failed")

    def fail(*args, **kwargs):
        ph._BUF.add("openrouter", same_provider, now=3.0)
        ph._BUF.add("other:new", new_provider, now=4.0)
        raise error

    targets = {
        "path": (ph, "_path"),
        "lock": (ph, "open"),
        "serialize": (ph.json, "dumps"),
        "write": (ph.Path, "write_text"),
        "replace": (ph.os, "replace"),
    }
    with monkeypatch.context() as patch:
        patch.setattr(*targets[failure_point], fail, raising=failure_point != "lock")
        with pytest.raises(type(error)) as exc:
            ph._BUF.flush(now=2.0)
        assert exc.value is error

    expected = {"openrouter": [first, same_provider], "modal:vllm": [second], "other:new": [new_provider]}
    assert ph._BUF.events == expected
    ph._BUF.flush(now=4.0)
    assert ph._BUF.events == {}
    assert json.loads(ph._path().read_text()) == expected


def test_cap_200_events_per_key(ph):
    for i in range(250):
        ph._BUF.add("openrouter", [float(i), True, 200, 0.1], now=float(i))
    ph._BUF.flush()
    assert len(json.loads(ph._path().read_text())["openrouter"]) == 200


def test_disabled_is_noop(monkeypatch, temp_base_dir):
    from llm import provider_health

    monkeypatch.setenv("MAILROOM_PROVIDER_HEALTH", "0")
    monkeypatch.setattr(provider_health, "_BUF", provider_health._Buffer())
    provider_health.record_llm_outcome("https://openrouter.ai/api/v1", "m", ok=False, status=500, latency_s=1, now=1.0)
    assert provider_health._BUF.events == {}


def test_retry_records_each_attempt(ph, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    from llm.retry import retry_chat_completion

    resp = MagicMock(status_code=503, headers={}, http_version="1.1")
    client = MagicMock()
    client.base_url = "https://acme--mailroom-vllm-extract.modal.run/v1"
    ok = object()
    client.chat.completions.create.side_effect = [APIStatusError("cold", response=resp, body=None), ok]
    assert retry_chat_completion(client, model="m", messages=[], max_attempts=3) is ok
    h = ph.read_provider_health()["modal:extract"]
    assert h["calls"] == 2 and h["count_503"] == 1 and h["consecutive_errors"] == 0
