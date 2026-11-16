"""#249: a chunked extraction whose every window failed is still a chunk failure.

Both chunked extractors (``agents.base.BaseAgent.extract_chunked`` and
``langchain_agents.specialist_agents._SpecialistBase.extract_chunked``) used to
return a bare ``{"_parse_error": True}`` when no window produced a result, with
no ``_n_chunks`` / ``_failed_chunks``. Routing and the review-cause collector read
those keys, so a total loss parked a document without ``chunk_failed``.
"""

from __future__ import annotations

import pytest

_FOUR_WINDOWS = ("x" * 90 + "\n\n") * 16  # chunk_chars=400, overlap=0 -> 4 windows


def _mailroom_agent(client):
    from agents.correspondence_specialist import CorrespondenceSpecialist

    agent = CorrespondenceSpecialist()
    agent.client = client
    agent.model = "test-model"
    return agent


def _vendored_agent(client):
    from agents.contracts_specialist import ContractsSpecialist

    agent = ContractsSpecialist()
    agent.client = client
    agent.model = "test-model"
    return agent


def _install(agent, behaviour):
    """``behaviour()`` is called once per window and returns a dict or raises."""
    if type(agent).__mro__[1].__module__.startswith("langchain_agents"):
        agent._call_structured = lambda *a, **k: behaviour()
    else:
        agent.extract = lambda *a, **k: behaviour()


def _raise_value_error():
    raise ValueError("boom")


_MAKERS = pytest.mark.parametrize("make", [_mailroom_agent, _vendored_agent])
_FAILURES = pytest.mark.parametrize(
    "behaviour",
    [_raise_value_error, lambda: {"_parse_error": True}],
    ids=["every-window-raises", "every-window-parse-error"],
)


@_MAKERS
@_FAILURES
def test_total_chunk_failure_carries_chunk_bookkeeping(make, behaviour, mock_openai_client):
    agent = make(mock_openai_client)
    _install(agent, behaviour)
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    assert result["_parse_error"] is True
    assert result["_n_chunks"] == 4
    assert result["_failed_chunks"] == 4
    assert agent._last_failed_chunks == 4


@_MAKERS
@_FAILURES
def test_total_chunk_failure_is_parked_as_chunk_failed(make, behaviour, mock_openai_client):
    from pipeline import reconsideration as rc

    agent = make(mock_openai_client)
    _install(agent, behaviour)
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    causes = rc.collect_review_causes({"doc_type": "contract", "extracted_data": result})
    assert rc.CHUNK_FAILED in causes


@_MAKERS
def test_total_chunk_failure_still_retries_then_goes_to_a_person(make, mock_openai_client):
    from graph.routing import after_extraction

    agent = make(mock_openai_client)
    _install(agent, lambda: {"_parse_error": True})
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    state = {
        "doc_type": "contract",
        "extracted_data": result,
        "extraction_confidence": 0.0,
        "extraction_attempts": 1,
    }
    assert after_extraction(state) == "retry_extract"
    assert after_extraction({**state, "extraction_attempts": 3}) == "human_review"


@_MAKERS
def test_transient_error_is_still_reraised(make, mock_openai_client):
    import httpx
    import openai

    agent = make(mock_openai_client)

    def _transient():
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://provider.invalid"))

    _install(agent, _transient)
    with pytest.raises(openai.APIConnectionError):
        agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)


@_MAKERS
def test_single_window_parse_error_is_unchanged(make, mock_openai_client):
    """A one-window document takes the plain path: no chunk bookkeeping is invented."""
    agent = make(mock_openai_client)
    agent.extract = lambda *a, **k: {"_parse_error": True}
    agent._call_structured = lambda *a, **k: {"_parse_error": True}
    result = agent.extract_chunked("short", chunk_chars=90_000, overlap_chars=0)
    assert "_failed_chunks" not in result
    assert "_n_chunks" not in result
