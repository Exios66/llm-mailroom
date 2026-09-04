"""Free-swarm failover (live-verified 2026-09-04): when a free model's
upstream pool rate-limits, the retry ladder rotates to the next taxonomy
`free_model_swarm:` entry instead of burning every attempt on one saturated
pool. Paid models never rotate. Hermetic — no real network calls."""

import time
from unittest.mock import MagicMock

import pytest
from openai import APIConnectionError, RateLimitError

SWARM = [
    "z-ai/glm-5.2:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "nvidia/nemotron-3.5-lightning:free",
]


def _http_response(status: int):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.http_version = "1.1"
    return resp


def _rl() -> RateLimitError:
    return RateLimitError(
        "rate limited", response=_http_response(429), body={"message": "rate limited"}
    )


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def swarm(monkeypatch):
    import llm.retry as retry

    monkeypatch.setattr(retry, "_free_swarm", lambda: list(SWARM))


class _RotatingClient:
    """Fakes `client.chat.completions.create`, recording the model per call."""

    def __init__(self, fail_for: set[str]):
        self.calls: list[str] = []
        self.fail_for = fail_for
        self.ok = object()

    @property
    def chat(self):
        outer = self

        class _Chat:
            @property
            def completions(self):
                class _Completions:
                    def create(self, **kwargs):
                        outer.calls.append(kwargs["model"])
                        if kwargs["model"] in outer.fail_for:
                            raise _rl()
                        return outer.ok

                return _Completions()

        return _Chat()


def test_rotates_to_next_free_model_on_rate_limit(no_sleep, swarm):
    from llm.retry import retry_chat_completion

    client = _RotatingClient(fail_for={SWARM[0]})
    result = retry_chat_completion(
        client, model=SWARM[0], messages=[], max_attempts=3, name="t"
    )
    assert result is client.ok
    assert client.calls == [SWARM[0], SWARM[1]]


def test_rotation_walks_the_swarm_then_parks(no_sleep, swarm):
    from llm.retry import retry_chat_completion

    client = _RotatingClient(fail_for=set(SWARM))
    with pytest.raises(RateLimitError):
        retry_chat_completion(client, model=SWARM[0], messages=[], max_attempts=5)
    # primary, then the remaining swarm entries, then the last one repeats
    assert client.calls == [SWARM[0], SWARM[1], SWARM[2], SWARM[2], SWARM[2]]


def test_no_rotation_for_paid_models(no_sleep, swarm):
    from llm.retry import retry_chat_completion

    paid = "acme/paid-model"
    client = _RotatingClient(fail_for={paid})
    with pytest.raises(RateLimitError):
        retry_chat_completion(client, model=paid, messages=[], max_attempts=3)
    assert client.calls == [paid, paid, paid]


def test_no_rotation_on_connection_errors(no_sleep, swarm, monkeypatch):
    from llm.retry import retry_chat_completion

    monkeypatch.setattr(
        "llm.retry._is_retryable", lambda exc: isinstance(exc, APIConnectionError)
    )

    class _ConnClient:
        def __init__(self):
            self.calls = []

        @property
        def chat(self):
            outer = self

            class _Chat:
                @property
                def completions(self):
                    class _Completions:
                        def create(self, **kwargs):
                            outer.calls.append(kwargs["model"])
                            raise APIConnectionError(request=object())

                    return _Completions()

            return _Chat()

    client = _ConnClient()
    with pytest.raises(APIConnectionError):
        retry_chat_completion(client, model=SWARM[0], messages=[], max_attempts=3)
    assert client.calls == [SWARM[0], SWARM[0], SWARM[0]]


def test_is_free_model_predicate():
    from llm.client import is_free_model

    assert is_free_model("z-ai/glm-5.2:free")  # registered 0/0 in taxonomy
    assert is_free_model("totally/unknown:free")  # :free suffix convention
    assert not is_free_model("qwen/qwen3.7-flash")  # registered paid
    assert not is_free_model("totally/unknown")  # unregistered, no suffix


def test_swarm_config_guarded_in_taxonomy():
    from pipeline.config import load_config

    swarm = load_config().get("free_model_swarm")
    assert isinstance(swarm, list) and len(swarm) >= 3
    assert swarm[0] == "z-ai/glm-5.2:free"
