"""LLM result cache (llm/result_cache.py) and its use by the Gmail triage agent."""

import json
from unittest.mock import MagicMock

import pytest

from llm import result_cache


@pytest.fixture
def cache_on(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_LLM_CACHE", "1")


def test_cache_key_changes_with_prompt():
    a = result_cache.cache_key("m", "prompt-1", "doc")
    assert a == result_cache.cache_key("m", "prompt-1", "doc")
    assert a != result_cache.cache_key("m", "prompt-2", "doc")
    assert a != result_cache.cache_key("m2", "prompt-1", "doc")
    assert result_cache.cache_key("m", "ab", "c") != result_cache.cache_key("m", "a", "bc")


def test_cache_roundtrip_and_ttl_expiry(cache_on):
    result_cache.put("k", {"x": 1}, now=0)
    assert result_cache.get("k", now=10) == {"x": 1}
    assert result_cache.get("k", now=result_cache.TTL_SECONDS + 1) is None
    assert result_cache.get("k", now=11) is None  # expired row was deleted


def test_cache_disabled_by_env(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_LLM_CACHE", "0")
    result_cache.put("k", {"x": 1})
    assert result_cache.get("k") is None
    assert not result_cache._db_path().exists()


def test_result_cache_hit_skips_llm(cache_on, mock_openai_client, sample_insurance_claim_text):
    from agents.gmail_triage import GmailTriageAgent

    choice = MagicMock()
    choice.message.content = json.dumps(
        {"primary_doc_class": "insurance_claim", "confidence": 0.9, "gist": "FNOL", "keywords": ["hail"]}
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]
    calls = []
    real_create = mock_openai_client.chat.completions.create

    def counting_create(**kwargs):
        calls.append(kwargs)
        return real_create(**kwargs)

    mock_openai_client.chat.completions.create = counting_create
    first = GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    n = len(calls)
    assert n >= 1
    mock_openai_client.chat.completions.create = counting_create
    second = GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    assert len(calls) == n
    assert second["primary_doc_class"] == first["primary_doc_class"] == "insurance_claim"
    assert second["debug"]["cache"] == "hit"


def test_unparseable_result_is_not_cached(cache_on, mock_openai_client, sample_insurance_claim_text):
    from agents.gmail_triage import GmailTriageAgent

    choice = MagicMock()
    choice.message.content = "not json at all"
    mock_openai_client.chat.completions.create.return_value.choices = [choice]
    out = GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    assert out["debug"]["parse_ok"] is False
    again = GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    assert "cache" not in again["debug"]


def test_put_sweeps_expired_rows_never_read_again(cache_on):
    result_cache.put("old", {"x": 1}, now=0)
    result_cache.put("new", {"x": 2}, now=result_cache.TTL_SECONDS + 10)
    with result_cache._db() as conn:
        keys = [k for (k,) in conn.execute("SELECT key FROM results")]
    assert keys == ["new"]


def test_triage_cache_key_covers_schema_and_skills(cache_on, mock_openai_client, monkeypatch, sample_insurance_claim_text):
    import agents.gmail_triage as triage_mod
    from agents.gmail_triage import GmailTriageAgent

    choice = MagicMock()
    choice.message.content = json.dumps(
        {"primary_doc_class": "insurance_claim", "confidence": 0.9, "gist": "FNOL", "keywords": ["hail"]}
    )
    mock_openai_client.chat.completions.create.return_value.choices = [choice]
    keys = []
    real_key = result_cache.cache_key
    monkeypatch.setattr(result_cache, "cache_key", lambda *a: keys.append(real_key(*a)) or keys[-1])

    GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    monkeypatch.setattr(GmailTriageAgent, "_skill_appendix", lambda self: "\n\nnew skill text")
    GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    schema = {**triage_mod.TRIAGE_SCHEMA, "description": "changed"}
    monkeypatch.setattr(triage_mod, "TRIAGE_SCHEMA", schema)
    GmailTriageAgent().triage(sample_insurance_claim_text, filename="a.txt")
    assert len(keys) == 3 and len(set(keys)) == 3
