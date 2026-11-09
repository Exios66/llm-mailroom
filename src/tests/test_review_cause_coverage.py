"""Every route into ``human_review`` carries a machine-readable cause.

Routing functions return only a destination, so ``collect_review_causes``
rebuilds the reason from state. These tests enumerate the routes in
``graph/routing.py``: a hand-built state per route, plus a seeded sweep that
asserts the property "router says human_review -> at least one cause".
"""

from __future__ import annotations

import itertools
import random

import pytest

from graph import routing
from pipeline import reconsideration as rc
from pipeline.reconsideration import collect_review_causes

_HOLLOW = {"confidence": 0.9}
_INVALID = {"parties": 12345, "effective_date": ["not", "a", "date"]}


def _transient(extra: dict | None = None) -> dict:
    state = {
        "doc_type": "contract",
        "transient_error": True,
        "transient_retries_classify": 5,
        "transient_retries_retry_classify": 5,
        "transient_retries_review_classify": 5,
        "transient_retries_extract": 5,
        "transient_retries_retry_extract": 5,
        "transient_retries_judge_verify": 5,
        "transient_retries_arbiter": 5,
        "transient_retries_boss_escalation": 5,
    }
    state.update(extra or {})
    return state


# name -> (router, state, tokens that must be in the cause list)
ROUTES = {
    "classify_transient": (routing.after_classify, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "retry_classify_transient": (
        routing.after_retry_classify, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "extract_transient": (routing.after_extraction, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "retry_extract_transient": (
        routing.after_retry_extraction, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "boss_transient": (routing.after_boss, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "judge_transient": (routing.after_judge, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "arbiter_transient": (routing.after_arbiter, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "review_classify_transient": (
        routing.after_review_classify, _transient(), {rc.TRANSIENT_EXHAUSTED}),
    "classify_unknown_class": (
        routing.after_classify,
        {"doc_type": "unknown", "classification_confidence": 0.99, "classification_attempts": 1},
        {rc.UNKNOWN_CLASS},
    ),
    "classify_empty_text": (
        routing.after_classify,
        {"doc_type": "unknown", "doc_text": "  ", "classification_confidence": 0.1,
         "classification_attempts": 9},
        {rc.UNKNOWN_CLASS, rc.EMPTY_DOCUMENT},
    ),
    "retry_classify_unknown_class": (
        routing.after_retry_classify,
        {"doc_type": "not_a_class", "classification_confidence": 0.9, "classification_attempts": 3},
        {rc.UNKNOWN_CLASS},
    ),
    "classify_low_confidence_exhausted": (
        routing.after_classify,
        {"doc_type": "contract", "classification_confidence": 0.3, "classification_attempts": 5},
        {rc.SORTER_LOW_CONFIDENCE, rc.RETRIES_EXHAUSTED},
    ),
    "classify_medium_confidence_exhausted": (
        routing.after_classify,
        {"doc_type": "contract", "classification_confidence": 0.93, "classification_attempts": 5},
        {rc.RETRIES_EXHAUSTED},
    ),
    "extract_unsupported_stub": (
        routing.after_extraction,
        {"doc_type": "contract", "extracted_data": {"_unsupported": True},
         "extraction_confidence": 0.0, "extraction_attempts": 4,
         "extraction_guardrail": ["no_specialist_dispatch"]},
        {rc.UNSUPPORTED_EXTRACTION, rc.GUARDRAIL},
    ),
    "boss_does_not_clear_conflict": (
        routing.after_boss,
        {"doc_type": "contract", "conflict_detected": True, "review_decision": "review"},
        {rc.BOSS_CONFLICT},
    ),
    "extract_schema_invalid_exhausted": (
        routing.after_extraction,
        {"doc_type": "contract", "extracted_data": _INVALID, "extraction_confidence": 0.99,
         "extraction_attempts": 5},
        {rc.SCHEMA_INVALID, rc.RETRIES_EXHAUSTED},
    ),
    "extract_hollow_exhausted": (
        routing.after_extraction,
        {"doc_type": "contract", "extracted_data": {"confidence": 0.9},
         "extraction_confidence": 0.9, "extraction_attempts": 5},
        {rc.HOLLOW_EXTRACTION, rc.RETRIES_EXHAUSTED},
    ),
    "extract_low_confidence_no_judge": (
        routing.after_extraction,
        {"doc_type": "contract", "extracted_data": _INVALID, "extraction_confidence": 0.3,
         "extraction_attempts": 5, "extraction_guardrail": ["fabricated_value"]},
        {rc.LOW_EXTRACTION_CONFIDENCE, rc.GUARDRAIL},
    ),
    "judge_error": (
        routing.after_judge,
        {"doc_type": "contract", "judge_verdict": "judge_error", "extraction_confidence": 0.9},
        {rc.JUDGE_ERROR},
    ),
    "judge_max_passes_incomplete": (
        routing.after_judge,
        {"doc_type": "contract", "judge_verdict": "incomplete", "judge_pass_count": 3,
         "extraction_confidence": 0.9},
        {rc.JUDGE_INCOMPLETE, rc.RETRIES_EXHAUSTED},
    ),
    "judge_max_passes_partial": (
        routing.after_judge,
        {"doc_type": "contract", "judge_verdict": "partial", "judge_pass_count": 3,
         "extraction_confidence": 0.9},
        {rc.JUDGE_PARTIAL, rc.RETRIES_EXHAUSTED},
    ),
    "judge_complete_but_unsure": (
        routing.after_judge,
        {"doc_type": "contract", "judge_verdict": "complete", "extraction_confidence": 0.4,
         "extraction_attempts": 3, "extracted_data": _INVALID},
        {rc.LOW_EXTRACTION_CONFIDENCE},
    ),
    "arbiter_escalates": (
        routing.after_arbiter,
        {"doc_type": "contract", "arbiter_decision": "human_review", "extraction_confidence": 0.9},
        {rc.ARBITER_ESCALATED},
    ),
    "arbiter_retry_bound_spent": (
        routing.after_arbiter,
        {"doc_type": "contract", "arbiter_decision": "retry_extraction", "arbiter_retry_count": 3,
         "extraction_confidence": 0.9},
        {rc.ARBITER_ESCALATED, rc.RETRIES_EXHAUSTED},
    ),
    "arbiter_caveats_but_unsure": (
        routing.after_arbiter,
        {"doc_type": "contract", "arbiter_decision": "accept_with_caveats",
         "judge_verdict": "partial", "extraction_confidence": 0.4, "extraction_attempts": 3,
         "extracted_data": _INVALID},
        {rc.LOW_EXTRACTION_CONFIDENCE},
    ),
    "review_classify_conflicts": (
        routing.after_review_classify,
        {"doc_type": "contract", "classification_confidence": 0.8,
         "review_verdict": "reviewer_conflicts", "reviewer_doc_type": "invoice",
         "reviewer_confidence": 0.9},
        {rc.REVIEWER_DISAGREEMENT},
    ),
    "review_classify_agrees_low": (
        routing.after_review_classify,
        {"doc_type": "contract", "review_verdict": "reviewer_agrees_low",
         "reviewer_doc_type": "contract", "reviewer_confidence": 0.5},
        {rc.REVIEWER_LOW_CONFIDENCE},
    ),
    "review_classify_error": (
        routing.after_review_classify,
        {"doc_type": "contract", "review_verdict": "reviewer_error"},
        {rc.REVIEWER_UNAVAILABLE},
    ),
    "review_classify_decision_lane": (
        routing.after_review_classify,
        {"doc_type": "contract", "review_route": "human_review",
         "reviewer_causes": [rc.REVIEWER_NO_TEXT]},
        {rc.REVIEWER_NO_TEXT},
    ),
    "classification_arbiter_declines": (
        routing.after_classification_arbiter,
        {"doc_type": "contract", "classification_arbiter_decision": "human_review"},
        {rc.CLASS_ARBITER_ESCALATED},
    ),
    "report_failed": (
        routing.after_report,
        {"doc_type": "contract", "report_error": True,
         "extracted_data": {"_report": {"error": True}}},
        {rc.REPORTING_INCOMPLETE},
    ),
}


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_each_route_into_human_review_names_its_cause(name):
    router, state, expected = ROUTES[name]
    assert router(dict(state)) == "human_review", f"fixture {name} no longer routes to review"
    causes = collect_review_causes(state)
    assert causes, f"{name}: no cause recorded"
    missing = expected - set(causes)
    assert not missing, f"{name}: missing {sorted(missing)}; got {causes}"


def test_every_new_token_has_a_label():
    for token in (
        rc.TRANSIENT_EXHAUSTED, rc.BOSS_CONFLICT, rc.ARBITER_ESCALATED,
        rc.CLASS_ARBITER_ESCALATED, rc.UNKNOWN_CLASS, rc.EMPTY_DOCUMENT,
        rc.UNSUPPORTED_EXTRACTION, rc.LOW_EXTRACTION_CONFIDENCE, rc.JUDGE_INCOMPLETE,
        rc.JUDGE_ERROR, rc.RETRIES_EXHAUSTED, rc.CHUNK_FAILED,
    ):
        assert token in rc.CAUSE_LABELS


def test_legacy_tokens_are_unchanged():
    legacy = {
        "class_miss", "subclass_miss", "extraction_miss", "judge_miss", "judge_partial",
        "schema_invalid", "guardrail", "parse_error", "reporting_incomplete",
        "needs_judge_review", "hollow_extraction", "sorter_low_confidence",
        "reviewer_unavailable", "reviewer_no_text", "reviewer_ineligible_label",
        "reviewer_disagreement", "reviewer_low_confidence",
    }
    assert legacy <= set(rc.CAUSE_LABELS)


def test_judge_incomplete_is_no_longer_reporting_incomplete():
    causes = collect_review_causes({"doc_type": "contract", "judge_verdict": "incomplete"})
    assert rc.JUDGE_INCOMPLETE in causes
    assert rc.REPORTING_INCOMPLETE not in causes
    # The reporter's own completeness score still maps to REPORTING_INCOMPLETE.
    scored = collect_review_causes({}, scores={"completeness_label": "INCOMPLETE"})
    assert rc.REPORTING_INCOMPLETE in scored


def test_schema_invalid_fires_without_langfuse_scores():
    state = {"doc_type": "contract", "extracted_data": _INVALID, "extraction_confidence": 0.99,
             "extraction_attempts": 1}
    assert rc.SCHEMA_INVALID in collect_review_causes(state)


def test_parse_error_is_not_double_labelled_as_schema_invalid():
    state = {"doc_type": "contract", "extracted_data": {"_parse_error": "bad json"}}
    causes = collect_review_causes(state)
    assert rc.PARSE_ERROR in causes
    assert rc.SCHEMA_INVALID not in causes


@pytest.mark.parametrize("value", [2, ["a", "b"]])
def test_chunk_failed_reads_failed_chunks(value):
    state = {"doc_type": "contract", "extracted_data": {"parties": ["A"], "_failed_chunks": value}}
    assert rc.CHUNK_FAILED in collect_review_causes(state)
    clean = {"doc_type": "contract", "extracted_data": {"parties": ["A"], "_failed_chunks": 0}}
    assert rc.CHUNK_FAILED not in collect_review_causes(clean)


def test_clean_archive_path_state_gets_no_new_causes():
    """A confident, conflict-free run is not 'reconsidered' by the new causes."""
    state = {
        "doc_type": "contract",
        "classification_confidence": 0.99,
        "classification_attempts": 1,
        "extraction_confidence": 0.99,
        "extraction_attempts": 1,
        "conflict_detected": False,
        "transient_error": False,
        "judge_verdict": "complete",
        "judge_source_verified": True,
        "arbiter_decision": "accept_with_caveats",
    }
    assert collect_review_causes(state) == []


def test_boss_approval_clears_the_conflict_cause():
    state = {"doc_type": "contract", "conflict_detected": True, "review_decision": "approved"}
    assert rc.BOSS_CONFLICT not in collect_review_causes(state)


def test_stale_arbiter_retry_is_not_an_escalation():
    state = {"doc_type": "contract", "arbiter_decision": "retry_extraction",
             "arbiter_retry_count": 1}
    assert rc.ARBITER_ESCALATED not in collect_review_causes(state)


# --------------------------------------------------------------------------
# Property-style sweep: sample coherent states, ask every router, and require a
# cause whenever the router parks the document.

_ROUTERS = [
    routing.after_classify,
    routing.after_retry_classify,
    routing.after_extraction,
    routing.after_extraction_gated,
    routing.after_retry_extraction,
    routing.after_retry_extraction_gated,
    routing.after_boss,
    routing.after_review_classify,
    routing.after_classification_arbiter,
    routing.after_judge,
    routing.after_arbiter,
    routing.after_report,
]


def sample_states(n: int = 4000, seed: int = 20260101) -> list[dict]:
    """Seeded state samples without ground truth, as in live runs."""
    rng = random.Random(seed)
    payloads = [None, {}, _HOLLOW, _INVALID, {"parties": ["A", "B"], "reasoning": "x"},
                {"_parse_error": "bad"}, {"_unsupported": True},
                {"parties": ["A"], "_failed_chunks": 1}]
    states: list[dict] = []
    for _ in range(n):
        state: dict = {
            "doc_id": "d",
            "doc_type": rng.choice(["contract", "contract", "contract", "unknown", "not_a_class"]),
            "classification_confidence": rng.choice([None, 0.2, 0.8, 0.93, 0.99]),
            "classification_attempts": rng.choice([1, 2, 5]),
            "extraction_confidence": rng.choice([None, 0.2, 0.8, 0.93, 0.99]),
            "extraction_attempts": rng.choice([0, 1, 2, 5]),
            "extracted_data": rng.choice(payloads),
            "conflict_detected": rng.random() < 0.15,
            "review_decision": rng.choice([None, "approved", "review"]),
        }
        if rng.random() < 0.2:
            state.update(_transient())
            state["doc_type"] = rng.choice(["contract", "unknown"])
        if rng.random() < 0.3:
            state["judge_verdict"] = rng.choice(
                ["complete", "partial", "incomplete", "judge_error", "skipped"])
            state["judge_pass_count"] = rng.choice([1, 3])
            state["extraction_attempts"] = max(state["extraction_attempts"], 3)
        if rng.random() < 0.25:
            state["arbiter_decision"] = rng.choice(
                ["accept_with_caveats", "retry_extraction", "human_review"])
            state["arbiter_retry_count"] = rng.choice([1, 3])
            state["judge_verdict"] = state.get("judge_verdict") or "partial"
            state["extraction_attempts"] = max(state["extraction_attempts"], 3)
        if rng.random() < 0.25:
            state["review_verdict"] = rng.choice(
                ["reviewer_agrees_high", "reviewer_agrees_low", "reviewer_conflicts",
                 "reviewer_overrides", "reviewer_error"])
            state["reviewer_doc_type"] = rng.choice(["contract", "invoice", "unknown"])
            state["reviewer_confidence"] = rng.choice([0.5, 0.99])
        if rng.random() < 0.1:
            state["review_route"] = rng.choice(["human_review", "extract", "classification_arbiter"])
            state["reviewer_causes"] = rng.choice(
                [[], [rc.REVIEWER_DISAGREEMENT], [rc.REVIEWER_LOW_CONFIDENCE]])
        if rng.random() < 0.1:
            state["classification_arbiter_decision"] = rng.choice(
                ["accept_sorter", "accept_reviewer", "human_review"])
        if rng.random() < 0.1:
            state["extraction_guardrail"] = ["x"]
        if rng.random() < 0.2:
            state["report_error"] = True
            state["extracted_data"] = {"parties": ["A"], "_report": {"error": True}}
        elif rng.random() < 0.2:
            state["extracted_data"] = {"parties": ["A"], "_report": {"summary": "ok"}}
        states.append(state)
    return states


# Routers that only ever see a document before extraction has started.
_CLASSIFICATION_STAGE = frozenset({
    "after_classify", "after_retry_classify", "after_review_classify",
    "after_classification_arbiter",
})
_EXTRACTION_KEYS = ("extracted_data", "extraction_confidence", "extraction_attempts",
                    "judge_verdict", "arbiter_decision", "report_error")


def _no_extraction(state: dict) -> bool:
    return not any(state.get(k) for k in _EXTRACTION_KEYS)


def _coherent_for(router, state: dict) -> bool:
    """A classification-stage router never sees extraction-stage state."""
    if router.__name__ == "after_boss":
        # The boss only runs on a detected conflict.
        return bool(state.get("conflict_detected"))
    if router.__name__ == "after_arbiter":
        return bool(state.get("arbiter_decision"))
    if router.__name__ == "after_judge":
        return bool(state.get("judge_verdict"))
    if router.__name__ == "after_report":
        return isinstance(state.get("extracted_data"), dict) and "_report" in state["extracted_data"]
    if router.__name__ in ("after_review_classify",):
        return bool(state.get("review_verdict") or state.get("review_route")) and _no_extraction(state)
    if router.__name__ == "after_classification_arbiter":
        return bool(state.get("classification_arbiter_decision")) and _no_extraction(state)
    if router.__name__ not in _CLASSIFICATION_STAGE:
        return True
    return _no_extraction(state)


def uncovered_parks(states=None, collect=collect_review_causes) -> tuple[int, list]:
    """Return (parks, uncovered) where uncovered is [(router name, state)]."""
    parks = 0
    uncovered = []
    for state in states if states is not None else sample_states():
        for router in _ROUTERS:
            if not _coherent_for(router, state):
                continue
            try:
                dest = router(dict(state))
            except Exception:
                continue
            if dest != "human_review":
                continue
            parks += 1
            if not collect(dict(state)):
                uncovered.append((router.__name__, state))
    return parks, uncovered


def test_sweep_every_park_has_a_cause():
    parks, uncovered = uncovered_parks()
    assert parks > 500, "sampler no longer exercises the human_review routes"
    assert not uncovered, (
        f"{len(uncovered)}/{parks} parks have no cause; first: {uncovered[0]}"
    )


def test_sweep_reaches_every_router():
    seen = set()
    for state in sample_states(1500):
        for router in _ROUTERS:
            try:
                if router(dict(state)) == "human_review":
                    seen.add(router.__name__)
            except Exception:
                continue
    assert seen >= {
        "after_classify", "after_retry_classify", "after_extraction", "after_boss",
        "after_review_classify", "after_judge", "after_arbiter", "after_report",
        "after_classification_arbiter",
    }, sorted(seen)


def test_property_grid_unknown_class_always_named():
    for conf, attempts in itertools.product([None, 0.1, 0.99], [1, 5]):
        state = {"doc_type": "unknown", "classification_confidence": conf,
                 "classification_attempts": attempts}
        assert routing.after_classify(dict(state)) == "human_review"
        assert rc.UNKNOWN_CLASS in collect_review_causes(state)
