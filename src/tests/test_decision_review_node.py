"""Decision-only branch of ``review_classify_node`` and its routing.

Network-free: the decision backend is a fake, and the LLM reviewer is patched
to raise so any fallback to it fails the test. Default config keeps every
class on ``llm``; these tests turn on ``decision_only`` for two classes.
"""

import copy

import httpx
import openai
import pytest

from agents.decision_backend import (
    get_decision_backend,
    register_decision_backend,
)
from graph.build_graph import review_classify_node
from graph.routing import after_review_classify
from pipeline.config import get_review_mode, get_reviewer_config
from pipeline.decision_review import DecisionAnswer
from pipeline.reconsideration import (
    CAUSE_LABELS,
    REVIEWER_DISAGREEMENT,
    REVIEWER_INELIGIBLE_LABEL,
    REVIEWER_LOW_CONFIDENCE,
    REVIEWER_NO_TEXT,
    REVIEWER_UNAVAILABLE,
    collect_review_causes,
)

DECISION_ONLY = {
    "insurance_claim": {"mode": "decision_only", "cutoff": 0.9},
    "corporate_record": {"mode": "decision_only", "cutoff": 0.9},
}


class Spy:
    """Fake decision backend that records calls."""

    def __init__(self, answer=None, exc=None):
        self.answer = answer
        self.exc = exc
        self.calls = []

    def __call__(self, doc_text, doc_types, subclass_catalogs):
        self.calls.append((doc_text, doc_types, subclass_catalogs))
        if self.exc is not None:
            raise self.exc
        return self.answer

    @property
    def call_count(self):
        return len(self.calls)


def answer(label="insurance_claim", subclass="carrier", conf=0.95, model="decision-model-1"):
    return DecisionAnswer(label, subclass, conf, model)


def state(**overrides):
    base = {
        "doc_id": "d1",
        "doc_text": "Claim number 123 for water damage.",
        "doc_type": "insurance_claim",
        "classification_confidence": 0.9,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_backend():
    register_decision_backend(None)
    yield
    register_decision_backend(None)


@pytest.fixture
def decision_config(monkeypatch):
    import pipeline.config as config

    cfg = copy.deepcopy(config.load_config())
    cfg["reviewer"] = {"default_mode": "llm", "by_class": copy.deepcopy(DECISION_ONLY)}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    return cfg


@pytest.fixture
def no_llm_reviewer(monkeypatch):
    """Any construction of the LLM reviewer fails the test."""
    import agents.sorter_reviewer as mod

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("LLM reviewer used in decision_only mode")

    monkeypatch.setattr(mod, "SorterReviewerAgent", Boom)


# A parked or failed decision review is escalated: the classification arbiter first, then a
# person (graph/routing.py _escalate_decision_review). Only a document with no text skips it.
ESCALATED = "classification_arbiter"


def route_after(st, update):
    return after_review_classify({**st, **update})


def test_default_config_keeps_every_class_on_llm():
    cfg = get_reviewer_config()
    assert cfg["default_mode"] == "llm"
    assert cfg["by_class"] == {}
    assert get_review_mode("insurance_claim") == "llm"
    assert get_review_mode(None) == "llm"


def test_by_class_entry_without_decision_only_is_llm(monkeypatch):
    import pipeline.config as config

    cfg = copy.deepcopy(config.load_config())
    cfg["reviewer"] = {
        "default_mode": "llm",
        "by_class": {"insurance_claim": {"cutoff": 0.9}, "contract": {"mode": "bogus"}},
    }
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    assert get_review_mode("insurance_claim") == "llm"
    assert get_review_mode("contract") == "llm"


@pytest.mark.parametrize("block", [None, [], "decision_only"])
def test_non_mapping_reviewer_config_defaults_to_llm(monkeypatch, block):
    import pipeline.config as config

    monkeypatch.setattr(config, "load_config", lambda: {"reviewer": block})
    assert get_reviewer_config() == {"default_mode": "llm", "by_class": {}}
    assert get_review_mode("insurance_claim") == "llm"


def test_class_entries_override_default_and_ignore_non_mappings(monkeypatch):
    import pipeline.config as config

    block = {
        "default_mode": "decision_only",
        "by_class": {
            "insurance_claim": {"mode": "llm"},
            "contract": {"cutoff": 0.9},
            "corporate_record": "invalid",
        },
    }
    monkeypatch.setattr(config, "load_config", lambda: {"reviewer": block})
    result = get_reviewer_config()
    assert "corporate_record" not in result["by_class"]
    assert get_review_mode("insurance_claim") == "llm"
    assert get_review_mode("contract") == "llm"
    assert get_review_mode("corporate_record") == "decision_only"
    result["by_class"]["insurance_claim"]["mode"] = "decision_only"
    assert get_review_mode("insurance_claim") == "llm"


@pytest.mark.parametrize("entries", [None, [], "invalid"])
def test_non_mapping_class_entries_use_default_mode(monkeypatch, entries):
    import pipeline.config as config

    monkeypatch.setattr(config, "load_config", lambda: {
        "reviewer": {"default_mode": "decision_only", "by_class": entries},
    })
    assert get_reviewer_config()["by_class"] == {}
    assert get_review_mode("insurance_claim") == "decision_only"


def test_registry_round_trip():
    assert get_decision_backend() is None
    spy = Spy()
    register_decision_backend(spy)
    assert get_decision_backend() is spy
    register_decision_backend(None)
    assert get_decision_backend() is None


def test_llm_mode_never_calls_backend(monkeypatch):
    import agents.sorter_reviewer as mod

    class FakeAgent:
        def review(self, doc_text, pages=None, **kw):
            return {
                "doc_type": "insurance_claim",
                "contract_subtype": None,
                "doc_subclass": "carrier",
                "confidence": 0.99,
                "reasoning": "clear",
            }

    monkeypatch.setattr(mod, "SorterReviewerAgent", FakeAgent)
    spy = Spy(answer=answer())
    register_decision_backend(spy)

    updates = review_classify_node(state())
    assert spy.call_count == 0
    assert updates["review_verdict"] == "reviewer_agrees_high"
    assert "review_route" not in updates
    assert "reviewer_causes" not in updates


def test_accept_routes_to_extract_with_zero_llm_calls(decision_config, no_llm_reviewer):
    spy = Spy(answer=answer(conf=0.96))
    register_decision_backend(spy)
    st = state()

    updates = review_classify_node(st)

    assert spy.call_count == 1
    doc_text, doc_types, catalogs = spy.calls[0]
    assert doc_text == st["doc_text"]
    assert "insurance_claim" in doc_types and doc_types == sorted(doc_types)
    assert "carrier" in catalogs["insurance_claim"]
    assert updates["review_route"] == "extract"
    assert updates["review_verdict"] == "decision_accepts"
    assert updates["reviewer_causes"] == []
    assert updates["reviewer_model"] == "decision-model-1"
    assert updates["doc_type"] == "insurance_claim"
    assert updates["doc_subclass"] == "carrier"
    assert updates["classification_confidence"] == 0.96
    assert updates["transient_error"] is False
    assert "sorter='insurance_claim'" in updates["escalation_reason"]
    assert route_after(st, updates) == "extract"


def test_disagreement_keeps_sorter_label_and_parks(decision_config, no_llm_reviewer):
    spy = Spy(answer=answer(label="corporate_record", subclass="bylaws", conf=1.0))
    register_decision_backend(spy)
    st = state()

    updates = review_classify_node(st)

    assert updates["review_route"] == "human_review"
    assert updates["review_verdict"] == "decision_escalates"
    assert updates["reviewer_causes"] == [REVIEWER_DISAGREEMENT]
    assert "doc_type" not in updates
    assert "doc_subclass" not in updates
    assert "classification_confidence" not in updates
    assert updates["reviewer_doc_type"] == "corporate_record"
    assert updates["reviewer_confidence"] == 1.0
    assert "reviewer_disagreement" in updates["escalation_reason"]
    assert route_after(st, updates) == ESCALATED


def test_backend_missing_parks_unavailable(decision_config, no_llm_reviewer):
    st = state()
    updates = review_classify_node(st)
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["reviewer_model"] is None
    assert "doc_type" not in updates
    assert route_after(st, updates) == ESCALATED


def test_permanent_error_parks_unavailable_without_llm(decision_config, no_llm_reviewer):
    spy = Spy(exc=RuntimeError("bad key"))
    register_decision_backend(spy)
    st = state()
    updates = review_classify_node(st)
    assert spy.call_count == 1
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["transient_error"] is False
    assert route_after(st, updates) == ESCALATED


def _transient():
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


def test_transient_error_uses_node_budget_then_unavailable(decision_config, no_llm_reviewer):
    spy = Spy(exc=_transient())
    register_decision_backend(spy)
    st = state()

    first = review_classify_node(st)
    assert first["transient_error"] is True
    assert first["transient_retries_review_classify"] == 1
    assert first["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert "review_route" not in first
    assert route_after(st, first) == "review_classify"

    exhausted = state(transient_retries_review_classify=3)
    last = review_classify_node(exhausted)
    assert last["transient_retries_review_classify"] == 4
    assert route_after(exhausted, last) == ESCALATED
    assert REVIEWER_UNAVAILABLE in collect_review_causes({**exhausted, **last})


def test_retry_after_transient_clears_unavailable(decision_config, no_llm_reviewer):
    register_decision_backend(Spy(answer=answer()))
    st = state(
        transient_error=True,
        transient_retries_review_classify=1,
        reviewer_causes=[REVIEWER_UNAVAILABLE],
    )
    updates = review_classify_node(st)
    assert updates["reviewer_causes"] == []
    assert updates["transient_error"] is False
    assert route_after(st, updates) == "extract"


@pytest.mark.parametrize("text", ["", "   \n", None])
def test_blank_text_skips_backend(decision_config, no_llm_reviewer, text):
    spy = Spy(answer=answer())
    register_decision_backend(spy)
    st = state(doc_text=text)
    updates = review_classify_node(st)
    assert spy.call_count == 0
    assert updates["review_route"] == "human_review"
    assert REVIEWER_NO_TEXT in updates["reviewer_causes"]
    assert REVIEWER_UNAVAILABLE in updates["reviewer_causes"]
    assert route_after(st, updates) == "human_review"


def test_causes_reach_collect_review_causes(decision_config, no_llm_reviewer):
    register_decision_backend(
        Spy(answer=answer(label="corporate_record", subclass="bylaws", conf=0.1))
    )
    st = state()
    updates = review_classify_node(st)
    causes = collect_review_causes({**st, **updates})
    assert REVIEWER_DISAGREEMENT in causes
    assert REVIEWER_LOW_CONFIDENCE in causes
    assert len(causes) == len(set(causes))


@pytest.mark.parametrize("container", [list, tuple])
def test_review_causes_filter_and_deduplicate_in_order(container):
    causes = collect_review_causes({
        "reviewer_causes": container([
            REVIEWER_DISAGREEMENT, "", None, 4, REVIEWER_LOW_CONFIDENCE,
            REVIEWER_DISAGREEMENT,
        ]),
    })
    assert causes == [REVIEWER_DISAGREEMENT, REVIEWER_LOW_CONFIDENCE]


@pytest.mark.parametrize("causes", [None, REVIEWER_DISAGREEMENT, {REVIEWER_DISAGREEMENT}])
def test_review_causes_ignore_non_sequence_containers(causes):
    assert collect_review_causes({"reviewer_causes": causes}) == []


@pytest.mark.parametrize(
    "bad",
    [
        "not an answer",
        DecisionAnswer(["insurance_claim"], "carrier", 0.99, "m"),  # type: ignore[arg-type]
        DecisionAnswer(7, "carrier", 0.99, "m"),  # type: ignore[arg-type]
        DecisionAnswer("insurance_claim", ["carrier"], 0.99, "m"),  # type: ignore[arg-type]
        DecisionAnswer("insurance_claim", "carrier", 0.99, None),  # type: ignore[arg-type]
    ],
)
def test_malformed_answer_is_no_answer(decision_config, no_llm_reviewer, bad):
    register_decision_backend(Spy(answer=bad))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert route_after(st, updates) == ESCALATED


def test_guard_exception_parks_as_unavailable(decision_config, no_llm_reviewer, monkeypatch):
    import pipeline.guards as guards

    def fail_guard(_state):
        raise ValueError("cannot inspect answer")

    monkeypatch.setattr(guards, "apply_classification_guard", fail_guard)
    register_decision_backend(Spy(answer=answer()))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["reviewer_model"] is None
    assert "doc_type" not in updates
    assert route_after(st, updates) == ESCALATED


def test_out_of_range_confidence_is_not_clamped_into_accept(decision_config, no_llm_reviewer):
    # A guard clamp to 0.5 would meet a 0.4 cutoff; an invalid value must not.
    decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"] = 0.4
    register_decision_backend(Spy(answer=answer(conf=7.0)))
    st = state()
    updates = review_classify_node(st)
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert route_after(st, updates) == ESCALATED


def test_guard_rejected_subclass_is_cleared_and_parks(decision_config, no_llm_reviewer):
    decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"] = 0.4
    register_decision_backend(Spy(answer=answer(subclass="not_a_subclass", conf=0.99)))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_doc_subclass"] is None
    assert updates["reviewer_confidence"] is None
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert "doc_subclass" not in updates


@pytest.mark.parametrize("cutoff", [0.5, 0.4, 0.0])
def test_guard_rejected_answer_parks_at_any_cutoff(decision_config, no_llm_reviewer, cutoff):
    # The guard clamps to 0.5, which would meet these cutoffs; a rejected
    # answer has no usable confidence at all.
    decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"] = cutoff
    register_decision_backend(Spy(answer=answer(subclass="not_a_subclass", conf=0.99)))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_confidence"] is None
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert route_after(st, updates) == ESCALATED
    assert "classification_confidence" not in updates


@pytest.mark.parametrize(
    "bad",
    ["high", [0.9], 7.0, -0.1, float("nan"), float("inf"), True, 10**400],
    ids=["text", "list", "above", "below", "nan", "inf", "bool", "huge"],
)
def test_invalid_confidence_is_none_in_state(decision_config, no_llm_reviewer, bad):
    decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"] = 0.4
    register_decision_backend(Spy(answer=answer(conf=bad)))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_confidence"] is None
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert route_after(st, updates) == ESCALATED


def test_oversized_int_cutoff_parks_through_node(decision_config, no_llm_reviewer):
    decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"] = 10**400
    register_decision_backend(Spy(answer=answer(conf=0.99)))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert route_after(st, updates) == ESCALATED


def test_ineligible_sorter_class_never_calls_backend(decision_config, no_llm_reviewer):
    # contract stays on llm in this config, so the LLM path (patched to raise)
    # handles it and the backend is untouched.
    spy = Spy(answer=answer())
    register_decision_backend(spy)
    updates = review_classify_node(state(doc_type="contract"))
    assert spy.call_count == 0
    assert updates["review_verdict"] == "reviewer_error"


def test_router_prefers_transient_check_over_route():
    st = {"transient_error": True, "transient_retries_review_classify": 1, "review_route": "extract"}
    assert after_review_classify(st) == "review_classify"


def test_router_unknown_route_value_parks():
    assert after_review_classify({"review_route": "archive"}) == ESCALATED


def test_router_without_route_keeps_llm_behavior():
    assert after_review_classify({"review_verdict": "reviewer_conflicts"}) == "human_review"


def test_accept_with_ground_truth_class_miss_parks(decision_config, no_llm_reviewer):
    # Model agrees with a wrong sorter label at/above cutoff: the LLM accept
    # arm would park this on a ground-truth miss, so the decision arm must too.
    register_decision_backend(Spy(answer=answer(conf=0.99)))
    st = state(ground_truth={"expected_doc_class": "corporate_record"})
    updates = review_classify_node(st)
    assert updates["review_route"] == "extract"
    assert route_after(st, updates) == "human_review"


def test_accept_with_matching_ground_truth_extracts(decision_config, no_llm_reviewer):
    register_decision_backend(Spy(answer=answer(conf=0.99)))
    st = state(ground_truth={"expected_doc_class": "insurance_claim"})
    updates = review_classify_node(st)
    assert route_after(st, updates) == "extract"


def test_accept_without_ground_truth_is_unchanged(decision_config, no_llm_reviewer):
    register_decision_backend(Spy(answer=answer(conf=0.99)))
    for gt in (None, {}, {"expected_doc_class": ""}):
        st = state() if gt is None else state(ground_truth=gt)
        updates = review_classify_node(st)
        assert route_after(st, updates) == "extract"


def test_router_route_extract_without_ground_truth_extracts():
    assert after_review_classify({"review_route": "extract", "reviewer_doc_type": "insurance_claim"}) == "extract"


@pytest.mark.parametrize("verdict", ["decision_accepts", "decision_escalates"])
@pytest.mark.parametrize("model", [None, "", "decision-model-1"])
def test_decision_model_attribution(verdict, model):
    from graph.build_graph import _resolved_models

    models = _resolved_models({"review_verdict": verdict, "reviewer_model": model})
    assert "sorter_reviewer=" not in models
    assert ("decision_reviewer=decision-model-1" in models) == bool(model)


def test_sorter_reviewer_attribution_still_uses_config():
    from graph.build_graph import _resolved_models

    assert "sorter_reviewer=" in _resolved_models({"review_verdict": "reviewer_agrees_high"})


def test_default_decision_only_without_entry_parks(decision_config, no_llm_reviewer):
    # default_mode selects the lane, but acceptance needs a by_class entry
    # with a tuned cutoff: an untuned class always goes to a person.
    decision_config["reviewer"] = {"default_mode": "decision_only", "by_class": {}}
    assert get_review_mode("insurance_claim") == "decision_only"
    register_decision_backend(Spy(answer=answer(conf=1.0)))
    st = state()
    updates = review_classify_node(st)
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_INELIGIBLE_LABEL, REVIEWER_LOW_CONFIDENCE]
    assert "classification_confidence" not in updates
    assert route_after(st, updates) == ESCALATED


def test_decision_only_entry_without_cutoff_parks(decision_config, no_llm_reviewer):
    del decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"]
    register_decision_backend(Spy(answer=answer(conf=1.0)))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert route_after(st, updates) == ESCALATED


def test_low_confidence_label_covers_every_way_to_get_it():
    label = CAUSE_LABELS[REVIEWER_LOW_CONFIDENCE]
    assert "below the class cutoff" in label
    assert "no valid cutoff" in label
    assert "missing" in label


@pytest.mark.parametrize(
    "backend_answer, overrides",
    [
        (None, {}),
        (answer(conf=0.1), {}),
        (answer(conf=10**400), {}),
        (answer(subclass="not_a_subclass"), {}),
        (answer(label="corporate_record", subclass="bylaws"), {}),
        (answer(), {"doc_text": ""}),
    ],
    ids=["no_answer", "low", "oversized", "guard_rejected", "disagreement", "no_text"],
)
def test_every_failure_is_escalated(
    decision_config, no_llm_reviewer, backend_answer, overrides
):
    """Every failure leaves the reviewer lane: to the classification arbiter, or with no
    text straight to a person."""
    register_decision_backend(Spy(answer=backend_answer))
    st = state(**overrides)
    updates = review_classify_node(st)
    assert updates["review_verdict"] == "decision_escalates"
    assert updates["reviewer_causes"]
    assert "classification_confidence" not in updates
    want = "human_review" if overrides.get("doc_text") == "" else ESCALATED
    assert route_after(st, updates) == want


@pytest.mark.parametrize(
    "text",
    [
        "[Unreadable file: scan.pdf]",
        "  [Unreadable file: a.docx — not a valid .docx]",
    ],
)
def test_unreadable_marker_text_skips_backend(decision_config, no_llm_reviewer, text):
    spy = Spy(answer=answer())
    register_decision_backend(spy)
    st = state(doc_text=text)
    updates = review_classify_node(st)
    assert spy.call_count == 0
    assert updates["review_route"] == "human_review"
    assert REVIEWER_NO_TEXT in updates["reviewer_causes"]
    assert route_after(st, updates) == "human_review"


def test_failed_extraction_error_skips_backend(decision_config, no_llm_reviewer):
    spy = Spy(answer=answer())
    register_decision_backend(spy)
    st = state(error_message="Could not extract text from .pdf file")
    updates = review_classify_node(st)
    assert spy.call_count == 0
    assert REVIEWER_NO_TEXT in updates["reviewer_causes"]


def test_stalled_backend_times_out_and_parks(decision_config, no_llm_reviewer, monkeypatch):
    import threading

    from pipeline import limits

    monkeypatch.setattr(limits, "get_call_timeout_seconds", lambda: 0.2)
    release = threading.Event()

    def stalled(doc_text, doc_types, catalogs):
        release.wait(10)
        return answer()

    register_decision_backend(stalled)
    st = state()
    try:
        updates = review_classify_node(st)
    finally:
        release.set()
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["transient_error"] is False
    assert "doc_type" not in updates
    assert route_after(st, updates) == ESCALATED


def test_backend_is_bounded_by_the_run_deadline(decision_config, no_llm_reviewer):
    import time

    from pipeline import limits

    spy = Spy(answer=answer())
    register_decision_backend(spy)
    limits.set_run_deadline(time.time() - 1)
    try:
        updates = review_classify_node(state())
    finally:
        limits.set_run_deadline(None)
    assert spy.call_count == 0
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["review_route"] == "human_review"


def test_readable_text_that_starts_with_marker_words_reaches_backend(
    decision_config, no_llm_reviewer
):
    spy = Spy(answer=answer())
    register_decision_backend(spy)
    st = state(doc_text="[Unreadable file: notes] Claim number 123 for water damage.")
    updates = review_classify_node(st)
    assert spy.call_count == 1
    assert REVIEWER_NO_TEXT not in updates["reviewer_causes"]


def test_abandoned_backend_threads_are_capped(decision_config, no_llm_reviewer, monkeypatch):
    import threading

    import graph.build_graph as bg
    from pipeline import limits

    monkeypatch.setattr(limits, "get_call_timeout_seconds", lambda: 0.05)
    monkeypatch.setattr(bg, "_MAX_ABANDONED_BACKEND_THREADS", 2)
    monkeypatch.setattr(bg, "_abandoned_backend_threads", [])
    release = threading.Event()
    started = []

    def stalled(doc_text, doc_types, catalogs):
        started.append(1)
        release.wait(10)
        return answer()

    register_decision_backend(stalled)
    try:
        for _ in range(5):
            updates = review_classify_node(state())
            assert updates["review_route"] == "human_review"
            assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
        # Two calls stalled and were tracked; the other three were refused.
        assert len(started) == 2
    finally:
        release.set()

    for thread in list(bg._abandoned_backend_threads):
        thread.join(2)
    # Finished threads are pruned, so calls are accepted again.
    register_decision_backend(Spy(answer=answer()))
    assert review_classify_node(state())["review_route"] == "extract"


class HandoffSpy(Spy):
    """Fake backend that accepts the sorter handoff."""

    def __call__(self, doc_text, doc_types, subclass_catalogs, handoff=None):
        self.handoff = handoff
        return super().__call__(doc_text, doc_types, subclass_catalogs)


def test_backend_accepting_handoff_receives_the_sorters_answer(decision_config, no_llm_reviewer):
    spy = HandoffSpy(answer=answer(conf=0.96))
    register_decision_backend(spy)
    st = state()

    review_classify_node(st)

    assert spy.call_count == 1
    assert spy.handoff["sorter_doc_type"] == st["doc_type"]
    assert spy.handoff["sorter_confidence"] == round(float(st["classification_confidence"]), 4)
    assert spy.handoff["review_trigger"][0] == "decision_class_review"


def test_three_argument_backend_gets_no_handoff(decision_config, no_llm_reviewer):
    spy = Spy(answer=answer(conf=0.96))
    register_decision_backend(spy)

    updates = review_classify_node(state())

    assert spy.call_count == 1
    assert updates["review_verdict"] == "decision_accepts"
