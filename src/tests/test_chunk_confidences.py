"""#255 (remaining item): each window's confidence is recorded on the merged extraction.

``finalize_chunked_merge`` stamps ``_chunk_confidences`` next to ``_n_chunks`` and
``_failed_chunks``: one entry per window in document order, ``None`` for a window that was
lost. Like the other underscore keys it is bookkeeping: field scoring, the hollow check and
the schema check ignore it.
"""

from __future__ import annotations

import pytest

_FOUR_WINDOWS = ("x" * 90 + "\n\n") * 16  # chunk_chars=400, overlap=0 -> 4 windows


def _vendored_agent(client):
    from agents.contracts_specialist import ContractsSpecialist

    agent = ContractsSpecialist()
    agent.client = client
    agent.model = "test-model"
    return agent


def _mailroom_agent(client):
    from agents.correspondence_specialist import CorrespondenceSpecialist

    agent = CorrespondenceSpecialist()
    agent.client = client
    agent.model = "test-model"
    return agent


def _install(agent, outcomes):
    """The i-th window returns ``outcomes[i]`` (a dict) or raises it (an exception)."""
    queue = list(outcomes)

    def call(*a, **k):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item)

    if type(agent).__mro__[1].__module__.startswith("langchain_agents"):
        agent._call_structured = call
    else:
        agent.extract = call


_MAKERS = pytest.mark.parametrize("make", [_vendored_agent, _mailroom_agent])


@_MAKERS
def test_every_window_confidence_is_recorded_in_document_order(make, mock_openai_client):
    agent = make(mock_openai_client)
    _install(agent, [{"confidence": c} for c in (0.9, 0.55, 0.8, 0.7)])
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    assert result["_chunk_confidences"] == [0.9, 0.55, 0.8, 0.7]
    assert result["_n_chunks"] == 4
    assert result["confidence"] == 0.55  # the merge rule itself is unchanged


@_MAKERS
def test_a_lost_window_is_recorded_as_none(make, mock_openai_client):
    agent = make(mock_openai_client)
    _install(agent, [{"confidence": 0.9}, ValueError("boom"), {"_parse_error": True}, {"confidence": 0.8}])
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    assert result["_chunk_confidences"] == [0.9, None, None, 0.8]
    assert result["_failed_chunks"] == 2


@_MAKERS
def test_total_failure_keeps_the_per_window_record(make, mock_openai_client):
    agent = make(mock_openai_client)
    _install(agent, [ValueError("boom")] * 4)
    result = agent.extract_chunked(_FOUR_WINDOWS, chunk_chars=400, overlap_chars=0)
    assert result["_chunk_confidences"] == [None] * 4


def test_single_window_documents_carry_no_chunk_record(mock_openai_client):
    agent = _vendored_agent(mock_openai_client)
    _install(agent, [{"confidence": 0.9}])
    result = agent.extract_chunked("short document", chunk_chars=400, overlap_chars=0)
    assert "_chunk_confidences" not in result


def test_finalize_chunked_merge_records_the_list_and_stays_optional():
    from langchain_agents.specialist_agents import finalize_chunked_merge

    with_record = finalize_chunked_merge(
        {"confidence": 0.8}, agent_name="contracts_specialist", n_chunks=2, failed=0,
        window_confidences=[0.8, 0.9],
    )
    assert with_record["_chunk_confidences"] == [0.8, 0.9]
    without = finalize_chunked_merge(
        {"confidence": 0.8}, agent_name="contracts_specialist", n_chunks=2, failed=0
    )
    assert "_chunk_confidences" not in without


def test_field_scoring_and_hollow_check_ignore_the_record():
    from observability.suite_scoring import score_with_suite
    from pipeline.reconsideration import extraction_is_hollow

    predicted = {"governing_law": "Delaware", "effective_date": "2021-03-05"}
    expected = {"governing_law": "Delaware", "effective_date": "2021-03-05"}
    plain, _ = score_with_suite("contract", predicted, expected)
    noted, _ = score_with_suite(
        "contract", {**predicted, "_chunk_confidences": [0.9, None]}, expected
    )
    assert noted.field_scores == plain.field_scores
    assert noted.overall_score == plain.overall_score
    assert extraction_is_hollow({"confidence": 0.9, "_chunk_confidences": [0.9]}) is True
