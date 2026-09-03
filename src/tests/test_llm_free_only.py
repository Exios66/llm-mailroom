"""MAILROOM_LLM_FREE_ONLY guardrail tests (HUB-039).

The pilot guardrail: with the flag on, ``get_llm`` refuses to resolve any
model that is not free (taxonomy ``cost_models`` prices both 0.0, or an
unregistered OpenRouter ``:free`` model) — BEFORE any client exists, so a
paid-model resolution can never become a paid request. Flag off ⇒ default
behavior, byte-for-byte unchanged.
"""

import pytest

from llm.client import assert_free_model, free_only_enabled, get_llm


@pytest.fixture
def free_only_on(monkeypatch):
    monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
    assert free_only_enabled()
    yield


@pytest.fixture
def free_only_off(monkeypatch):
    monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
    assert not free_only_enabled()
    yield


def test_flag_off_is_default(monkeypatch):
    monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
    assert free_only_enabled() is False
    monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "0")
    assert free_only_enabled() is False


def test_flag_on_parses(monkeypatch):
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", value)
        assert free_only_enabled() is True


def test_flag_off_paid_model_resolves(free_only_off):
    """Default behavior unchanged: the sorter's paid model resolves."""
    client, model = get_llm("sorter")
    assert model


def test_flag_on_free_agent_resolves(free_only_on):
    """The free triage team is exactly what the guardrail exists to allow."""
    client, model = get_llm("gmail_triage")
    assert model == "z-ai/glm-5.2:free"


def test_flag_on_paid_model_refused(free_only_on):
    with pytest.raises(RuntimeError, match="not free"):
        get_llm("sorter")


def test_flag_on_registered_zero_cost_is_free(free_only_on):
    assert_free_model("z-ai/glm-5.2:free")


def test_flag_on_unregistered_non_free_refused(free_only_on):
    """Unregistered + no ``:free`` suffix ⇒ not provably free ⇒ refused."""
    with pytest.raises(RuntimeError, match="not free"):
        assert_free_model("some-unregistered/model")


def test_flag_on_unregistered_free_suffix_allowed(free_only_on):
    assert_free_model("google/gemini-2.0-flash-exp:free")


def test_flag_off_assert_is_noop(free_only_off):
    assert_free_model("qwen/qwen3.7-flash")
