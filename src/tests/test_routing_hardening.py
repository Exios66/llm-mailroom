"""Routing-hardening regressions (F1-F4 + transient counter reset).

Each block pins one verified weak routing path: silent chunk loss with max
confidence (F1), sorter window disagreement (F2), conflict escalation ahead of
every quality gate (F3), exact-equality conflict detection (F4), and
``transient_retries_*`` counters that were documented as reset on success but
never were.
"""

from __future__ import annotations

import httpx
import openai
import pytest

# ---------------------------------------------------------------------------
# F1 - failed extraction chunks
# ---------------------------------------------------------------------------

_FOUR_WINDOWS = ("x" * 90 + "\n\n") * 16  # chunk_chars=400, overlap=0 -> 4 windows


def _conn_error() -> Exception:
    return openai.APIConnectionError(request=httpx.Request("POST", "http://provider.invalid"))


def _low(agent_name: str) -> float:
    from pipeline.config import get_confidence_thresholds, load_config

    lows = [
        float(get_confidence_thresholds(c["key"])["low"])
        for c in load_config()["doc_classes"]
        if c.get("specialist") == agent_name
    ]
    return min(lows)


def _mailroom_agent(mock_openai_client):
    """Mailroom-native path: ``agents.base.BaseAgent.extract_chunked``."""
    from agents.correspondence_specialist import CorrespondenceSpecialist

    agent = CorrespondenceSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    return agent


def _vendored_agent(mock_openai_client):
    """Vendored path: ``langchain_agents._SpecialistBase.extract_chunked``."""
    from agents.contracts_specialist import ContractsSpecialist

    agent = ContractsSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    return agent


def _sequence(outcomes):
    """Callable yielding each outcome in turn (raising exceptions)."""
    it = iter(outcomes)

    def _call(*args, **kwargs):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return dict(item)

    return _call


def _script(agent, outcomes):
    # BaseAgent path calls agent.extract per chunk; the vendored path calls
    # _call_structured per chunk.
    if type(agent).__mro__[1].__module__.startswith("langchain_agents"):
        agent._call_structured = _sequence(outcomes)
    else:
        agent.extract = _sequence(outcomes)


_MAKERS = pytest.mark.parametrize("make", [_mailroom_agent, _vendored_agent])


@_MAKERS
def test_f1_transient_chunk_error_is_reraised(make, mock_openai_client):
    agent = make(mock_openai_client)
    _script(agent, [{"confidence": 0.98}, _conn_error(), {"confidence": 0.9}, {"confidence": 0.9}])
    with pytest.raises(openai.APIConnectionError):
        agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)


@_MAKERS
def test_f1_failed_chunk_caps_confidence_below_low(make, mock_openai_client):
    agent = make(mock_openai_client)
    _script(
        agent,
        [
            {"confidence": 0.98},
            {"confidence": 0.97},
            {"_parse_error": True},
            {"confidence": 0.99},
        ],
    )
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    low = _low(agent.agent_name)
    assert result["_failed_chunks"] == 1
    assert result["_n_chunks"] == 4
    assert result["confidence"] < low, (result["confidence"], low)


@_MAKERS
def test_f1_non_transient_exception_chunk_counts_as_failed(make, mock_openai_client):
    agent = make(mock_openai_client)
    _script(
        agent,
        [{"confidence": 0.98}, ValueError("boom"), {"confidence": 0.99}, {"confidence": 0.99}],
    )
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    assert result["_failed_chunks"] == 1
    assert result["confidence"] < _low(agent.agent_name)


@_MAKERS
def test_f1_merged_confidence_is_min_not_max(make, mock_openai_client):
    agent = make(mock_openai_client)
    _script(agent, [{"confidence": c} for c in (0.98, 0.95, 0.60, 0.97)])
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    assert result["confidence"] == pytest.approx(0.60)
    assert result["_failed_chunks"] == 0
    assert result["_n_chunks"] == 4


def test_f1_single_chunk_unchanged(mock_openai_client):
    agent = _mailroom_agent(mock_openai_client)
    agent.extract = lambda *a, **k: {"claim": "1", "confidence": 0.8}
    assert agent.extract_chunked("short", chunk_chars=90_000, overlap_chars=0) == {
        "claim": "1",
        "confidence": 0.8,
    }


def test_f1_chunk_metadata_is_ignored_by_hollow_check():
    from pipeline.reconsideration import extraction_is_hollow

    assert extraction_is_hollow({"_failed_chunks": 1, "_n_chunks": 4, "confidence": 0.5})
