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


# ---------------------------------------------------------------------------
# F2 - sorter window disagreement lowers confidence
# ---------------------------------------------------------------------------


def _read(cls, conf=0.99, **extra):
    return {"doc_type": cls, "confidence": conf, "reasoning": f"r-{cls}", **extra}


def test_f2_three_way_disagreement_is_not_confident():
    from agents.sorter import _merge_sorter_reads
    from pipeline.config import get_confidence_thresholds

    merged = _merge_sorter_reads(
        [_read("contract"), _read("corporate_record"), _read("correspondence")]
    )
    # 1 agreeing of 3 non-unknown windows: 0.99 * 1/3
    assert merged["confidence"] == pytest.approx(0.33, abs=0.005)
    assert merged["confidence"] < get_confidence_thresholds(merged["doc_type"])["low"]


def test_f2_lone_class_among_unknown_windows_is_capped_below_low():
    from agents.sorter import _merge_sorter_reads
    from pipeline.config import get_confidence_thresholds

    merged = _merge_sorter_reads([_read("contract")] + [_read("unknown", 0.9)] * 4)
    assert merged["doc_type"] == "contract"
    assert merged["confidence"] < get_confidence_thresholds("contract")["low"]


def test_f2_unanimous_windows_keep_mean_confidence():
    from agents.sorter import _merge_sorter_reads

    merged = _merge_sorter_reads([_read("contract", 0.98), _read("contract", 0.96)])
    assert merged["doc_type"] == "contract"
    assert merged["confidence"] == pytest.approx(0.97)


def test_f2_majority_with_one_dissent_is_scaled_not_capped():
    from agents.sorter import _merge_sorter_reads

    merged = _merge_sorter_reads(
        [_read("contract", 0.99)] * 3 + [_read("correspondence", 0.99)]
    )
    # 3 agreeing of 4 non-unknown, winner holds >= half of all windows
    assert merged["doc_type"] == "contract"
    assert merged["confidence"] == pytest.approx(0.99 * 3 / 4, abs=0.005)


def test_f2_vote_vector_recorded_under_ignored_key():
    from agents.sorter import _merge_sorter_reads

    merged = _merge_sorter_reads([_read("contract"), _read("contract"), _read("unknown", 0.5)])
    assert merged["_window_votes"] == {"contract": 2, "unknown": 1}


# ---------------------------------------------------------------------------
# F3 - conflict escalation runs after every quality gate
# ---------------------------------------------------------------------------


def _conflict_state(**over):
    state = {
        "doc_type": "contract",
        "extracted_data": {"parties": ["Acme"], "governing_law": "Delaware"},
        "extraction_confidence": 0.99,
        "extraction_attempts": 1,
        "conflict_detected": True,
    }
    state.update(over)
    return state


def test_f3_conflict_with_schema_invalid_extraction_retries_not_boss():
    from graph.routing import after_extraction

    state = _conflict_state(
        extracted_data={"parties": "not-a-list"}, extraction_confidence=0.1
    )
    assert after_extraction(state) == "retry_extract"
    assert after_extraction({**state, "extraction_attempts": 3}) == "human_review"


def test_f3_conflict_with_low_confidence_retries_not_boss():
    from graph.routing import after_extraction

    state = _conflict_state(extraction_confidence=0.1)
    assert after_extraction(state) == "retry_extract"


def test_f3_conflict_with_hollow_extraction_retries_not_boss():
    from graph.routing import after_extraction

    state = _conflict_state(extracted_data={"confidence": 0.9, "_n_chunks": 2})
    assert after_extraction(state) == "retry_extract"


def test_f3_conflict_with_low_confidence_retries_spent_goes_to_review_or_judge():
    from graph.routing import after_extraction

    state = _conflict_state(extraction_confidence=0.1, extraction_attempts=3)
    assert after_extraction(state) in {"judge_verify", "human_review"}
    assert after_extraction(state) != "boss_escalation"


def test_f3_clean_conflict_still_escalates_to_boss():
    from graph.routing import after_extraction, after_extraction_gated

    state = _conflict_state()
    assert after_extraction(state) == "boss_escalation"
    assert after_extraction_gated(state) == "boss_escalation"


def test_f3_conflict_in_judge_band_still_escalates_to_boss():
    from graph.routing import after_extraction_gated

    # Ambiguous-band confidence (low <= c < judge_band_high): the Boss ruling
    # on the conflict was never judge-gated and still is not.
    state = _conflict_state(extraction_confidence=0.91)
    assert after_extraction_gated(state) == "boss_escalation"


def test_f3_no_conflict_paths_unchanged():
    from graph.routing import after_extraction

    assert after_extraction(_conflict_state(conflict_detected=False)) == "compile_report"


def test_f3_graph_edge_maps_still_valid():
    import typing

    from graph import routing
    from graph.build_graph import build_graph

    branches = build_graph().builder.branches
    for node, fn in (
        ("extract", routing.after_extraction_gated),
        ("retry_extract", routing.after_retry_extraction_gated),
    ):
        declared = set(typing.get_args(typing.get_type_hints(fn)["return"]))
        assert declared <= set(branches[node][fn.__name__].ends), (node, declared)


# ---------------------------------------------------------------------------
# F4 - conflict detection is not exact string equality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "a", "b"),
    [
        ("governing_law", "Delaware", "State of Delaware"),
        ("governing_law", "New York", "the laws of the State of New York"),
        ("governing_law", "Commonwealth of Virginia", "Virginia"),
        ("effective_date", "2024-01-05", "January 5, 2024"),
        ("effective_date", "2024-01-05", "5th January 2024"),
        ("agreement_date", "01/15/2024", "2024-01-15"),
        ("parties", ["Acme Corp", "Beta LLC"], ["acme corp.", "BETA LLC", "Gamma Inc"]),
    ],
)
def test_f4_equivalent_values_are_not_conflicts(field, a, b):
    from graph.build_graph import _normalized_compare

    assert _normalized_compare(a, b, field=field)
    assert _normalized_compare(b, a, field=field)


@pytest.mark.parametrize(
    ("field", "a", "b"),
    [
        ("governing_law", "Delaware", "New York"),
        ("governing_law", "State of New York", "State of New Jersey"),
        ("governing_law", "Virginia", "West Virginia"),
        ("effective_date", "2024-01-05", "2024-01-06"),
        ("effective_date", "2024-01-05", "January 6, 2024"),
        ("agreement_date", "01/02/2024", "2024-02-01"),
        ("parties", ["Acme Corp"], ["Beta LLC"]),
        ("document_name", "Master Services Agreement", "Lease Agreement"),
    ],
)
def test_f4_real_conflicts_still_fire(field, a, b):
    from graph.build_graph import _normalized_compare

    assert not _normalized_compare(a, b, field=field)


def test_f4_field_argument_is_optional():
    from graph.build_graph import _normalized_compare

    assert _normalized_compare("2024-01-05", "January 5, 2024")
    assert not _normalized_compare("Delaware", "New York")


def test_f4_detect_conflict_end_to_end(monkeypatch):
    from graph import build_graph as bg

    prior = {
        "doc_id": "old",
        "doc_type": "contract",
        "extracted_data": {"governing_law": "State of Delaware", "effective_date": "January 5, 2024"},
    }
    monkeypatch.setattr(bg, "_fetch_matter_context", lambda state: [prior])
    state = {"doc_type": "contract", "doc_id": "new", "matter_id": "m1"}
    same = {"governing_law": "Delaware", "effective_date": "2024-01-05"}
    assert bg._detect_conflict(state, same) == (False, [])
    differs = {"governing_law": "New York", "effective_date": "2024-01-05"}
    detected, details = bg._detect_conflict(state, differs)
    assert detected and "governing_law" in details[0]


# ---------------------------------------------------------------------------
# transient_retries_* counters: cumulative by design
# ---------------------------------------------------------------------------


def test_transient_counter_is_a_run_record_not_reset_on_success():
    """``first_pass_success`` reads the counters, so they must survive a
    successful call; the routing docstring must not claim otherwise."""
    from graph.routing import _transient_decision
    from observability.scores import first_pass_success

    archived = {"stage": "archived", "scores": None}
    assert first_pass_success(archived)
    assert not first_pass_success({**archived, "transient_retries_extract": 1})
    assert "NOT reset on success" in (_transient_decision.__doc__ or "")


def test_f2_single_window_path_is_identical():
    from agents.sorter import _merge_sorter_reads

    one = _read("contract", 0.91)
    assert _merge_sorter_reads([one]) is one


def test_conflict_on_judge_tail_never_archives_unadjudicated():
    from graph.routing import after_arbiter, after_judge

    base = {"doc_type": "contract", "conflict_detected": True, "judge_source_verified": True,
            "extraction_confidence": 0.99}
    assert after_judge({**base, "judge_verdict": "complete"}) == "human_review"
    assert after_arbiter({**base, "arbiter_decision": "accept_with_caveats"}) == "human_review"
    clean = {**base, "conflict_detected": False}
    assert after_judge({**clean, "judge_verdict": "complete"}) == "compile_report"
    assert after_arbiter({**clean, "arbiter_decision": "accept_with_caveats"}) == "compile_report"
