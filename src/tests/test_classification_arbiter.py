"""Classification arbiter: a parked or failed decision review goes to the arbiter, then a person.

Network-free: the arbiter agent is replaced by a stub.
"""

import copy

import pytest

import agents.arbiter as arbiter_mod
from graph.build_graph import classification_arbiter_node
from graph.routing import after_classification_arbiter, after_review_classify
from pipeline.config import get_reviewer_config
from pipeline.reconsideration import REVIEWER_LOW_CONFIDENCE, REVIEWER_NO_TEXT, REVIEWER_UNAVAILABLE


@pytest.fixture
def decision_config(monkeypatch):
    import pipeline.config as cfg

    real = cfg.get_reviewer_config()
    patched = copy.deepcopy(real)
    patched["by_class"]["insurance_claim"] = {"mode": "decision_only", "cutoff": 0.9}
    patched["by_class"]["corporate_record"] = {"mode": "decision_only", "cutoff": 0.9, "arbiter_cutoff": 0.6}
    monkeypatch.setattr(cfg, "get_reviewer_config", lambda: patched)
    return patched


def stub_arbiter(monkeypatch, result=None, exc=None):
    calls = []

    class Stub:
        def arbitrate_classification(self, **kwargs):
            calls.append(kwargs)
            if exc is not None:
                raise exc
            return result

    monkeypatch.setattr(arbiter_mod, "ClassificationArbiterAgent", Stub)
    return calls


def parked(**overrides):
    base = {
        "doc_id": "d1",
        "doc_text": "Claim number 123 for water damage.",
        "doc_type": "insurance_claim",
        "doc_subclass": "property",
        "classification_confidence": 0.8,
        "reviewer_doc_type": "corporate_record",
        "reviewer_doc_subclass": "bylaws",
        "reviewer_confidence": 0.5,
        "review_route": "human_review",
        "reviewer_causes": [REVIEWER_LOW_CONFIDENCE],
        "escalation_reason": "decision review: below cutoff",
    }
    base.update(overrides)
    return base


def test_parked_review_goes_to_the_arbiter(decision_config):
    assert after_review_classify(parked()) == "classification_arbiter"


def test_no_text_skips_the_arbiter(decision_config):
    assert after_review_classify(parked(reviewer_causes=[REVIEWER_NO_TEXT])) == "human_review"


def test_unavailable_decision_reviewer_is_escalated_to_the_arbiter(decision_config):
    st = parked(transient_error=True, transient_retries_review_classify=99,
                reviewer_causes=[REVIEWER_UNAVAILABLE], review_route=None)
    assert after_review_classify(st) == "classification_arbiter"


def test_arbiter_accepting_the_sorter_above_cutoff_continues(decision_config, monkeypatch):
    calls = stub_arbiter(monkeypatch, {"decision": "accept_sorter", "confidence": 0.99, "reasoning": "FNOL form"})
    st = parked()
    updates = classification_arbiter_node(st)
    assert calls and calls[0]["causes"] == [REVIEWER_LOW_CONFIDENCE]
    assert calls[0]["sorter"]["doc_type"] == "insurance_claim"
    assert updates["classification_arbiter_decision"] == "accept_sorter"
    assert updates["doc_type"] == "insurance_claim" and updates["classification_confidence"] == 0.99
    assert after_classification_arbiter({**st, **updates}) == "extract"


def test_arbiter_below_the_class_high_band_goes_to_a_person(decision_config, monkeypatch):
    stub_arbiter(monkeypatch, {"decision": "accept_sorter", "confidence": 0.7, "reasoning": "probably"})
    st = parked()
    updates = classification_arbiter_node(st)
    assert updates["classification_arbiter_decision"] == "human_review"
    assert "doc_type" not in updates
    assert "routing to human review" in updates["escalation_reason"]
    assert after_classification_arbiter({**st, **updates}) == "human_review"


def test_configured_arbiter_cutoff_is_used(decision_config, monkeypatch):
    stub_arbiter(monkeypatch, {"decision": "accept_reviewer", "confidence": 0.7, "reasoning": "bylaws"})
    updates = classification_arbiter_node(parked())
    assert updates["classification_arbiter_decision"] == "accept_reviewer"
    assert updates["doc_type"] == "corporate_record" and updates["doc_subclass"] == "bylaws"


@pytest.mark.parametrize("result", [
    {"decision": "human_review", "confidence": 0.99, "reasoning": "mixed"},
    {"decision": "accept_sorter", "confidence": "high", "reasoning": "x"},
    {"decision": "accept_reviewer", "confidence": 0.99, "reasoning": "x"},  # reviewer gave no class
])
def test_arbiter_human_or_unusable_answer_goes_to_a_person(decision_config, monkeypatch, result):
    stub_arbiter(monkeypatch, result)
    st = parked(reviewer_doc_type=None, reviewer_doc_subclass=None, reviewer_confidence=None)
    updates = classification_arbiter_node(st)
    assert updates["classification_arbiter_decision"] == "human_review"
    assert after_classification_arbiter({**st, **updates}) == "human_review"


def test_arbiter_failure_goes_to_a_person(decision_config, monkeypatch):
    stub_arbiter(monkeypatch, exc=RuntimeError("provider down"))
    st = parked()
    updates = classification_arbiter_node(st)
    assert updates["classification_arbiter_decision"] == "human_review"
    assert "classification arbiter failed (RuntimeError)" in updates["escalation_reason"]
    assert after_classification_arbiter({**st, **updates}) == "human_review"


def test_invalid_decision_from_the_agent_is_human_review(monkeypatch):
    agent = arbiter_mod.ClassificationArbiterAgent.__new__(arbiter_mod.ClassificationArbiterAgent)
    monkeypatch.setattr(agent, "_call_structured", lambda message, schema: {"decision": "archive", "confidence": 1.0},
                        raising=False)
    out = agent.arbitrate_classification(sorter={}, reviewer={}, causes=[], labels=["contract"], doc_text="x")
    assert out["decision"] == "human_review"


def test_reviewer_config_is_unchanged_by_default():
    assert "arbiter_cutoff" not in (get_reviewer_config()["by_class"].get("contract") or {})
