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
    REVIEWER_DISAGREEMENT,
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
    assert route_after(st, updates) == "human_review"


def test_backend_missing_parks_unavailable(decision_config, no_llm_reviewer):
    st = state()
    updates = review_classify_node(st)
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["reviewer_model"] is None
    assert "doc_type" not in updates
    assert route_after(st, updates) == "human_review"


def test_permanent_error_parks_unavailable_without_llm(decision_config, no_llm_reviewer):
    spy = Spy(exc=RuntimeError("bad key"))
    register_decision_backend(spy)
    st = state()
    updates = review_classify_node(st)
    assert spy.call_count == 1
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert updates["transient_error"] is False
    assert route_after(st, updates) == "human_review"


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
    assert route_after(exhausted, last) == "human_review"
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


@pytest.mark.parametrize(
    "bad",
    [
        "not an answer",
        DecisionAnswer(["insurance_claim"], "carrier", 0.99, "m"),  # type: ignore[arg-type]
        DecisionAnswer(7, "carrier", 0.99, "m"),  # type: ignore[arg-type]
    ],
)
def test_malformed_answer_is_no_answer(decision_config, no_llm_reviewer, bad):
    register_decision_backend(Spy(answer=bad))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_causes"] == [REVIEWER_UNAVAILABLE]
    assert route_after(st, updates) == "human_review"


def test_out_of_range_confidence_is_not_clamped_into_accept(decision_config, no_llm_reviewer):
    # A guard clamp to 0.5 would meet a 0.4 cutoff; an invalid value must not.
    decision_config["reviewer"]["by_class"]["insurance_claim"]["cutoff"] = 0.4
    register_decision_backend(Spy(answer=answer(conf=7.0)))
    st = state()
    updates = review_classify_node(st)
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert route_after(st, updates) == "human_review"


def test_guard_rejected_subclass_is_cleared_and_parks(decision_config, no_llm_reviewer):
    register_decision_backend(Spy(answer=answer(subclass="not_a_subclass", conf=0.99)))
    st = state()
    updates = review_classify_node(st)
    assert updates["reviewer_doc_subclass"] is None
    assert updates["review_route"] == "human_review"
    assert updates["reviewer_causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert "doc_subclass" not in updates


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
    assert after_review_classify({"review_route": "archive"}) == "human_review"


def test_router_without_route_keeps_llm_behavior():
    assert after_review_classify({"review_verdict": "reviewer_conflicts"}) == "human_review"
