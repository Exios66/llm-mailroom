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


def _agent_with(monkeypatch, replies, window_chars):
    """An arbiter whose model answers ``replies`` in order, reading ``window_chars`` per call."""
    agent = arbiter_mod.ClassificationArbiterAgent.__new__(arbiter_mod.ClassificationArbiterAgent)
    seen = []

    def call(message, schema):
        seen.append(message)
        return dict(replies[len(seen) - 1])

    monkeypatch.setattr(agent, "_call_structured", call, raising=False)
    monkeypatch.setattr(agent, "_configured_max_input_chars", lambda: window_chars, raising=False)
    return agent, seen


def test_long_document_is_arbitrated_from_every_window(monkeypatch):
    """Past one window the arbiter reads all of the text; it no longer hands a long document to a person unread."""
    yes = {"decision": "accept_sorter", "confidence": 0.9, "reasoning": "FNOL"}
    paragraphs = [f"Paragraph {i} " + "word " * 200 for i in range(20)]
    text = "\n\n".join(paragraphs)
    agent, seen = _agent_with(monkeypatch, [yes, {**yes, "confidence": 0.7}, yes, yes, yes, yes, yes, yes], 4000)
    out = agent.arbitrate_classification(sorter={}, reviewer={}, causes=[], labels=["contract"], doc_text=text)
    assert len(seen) > 1
    assert all(p in "".join(seen) for p in paragraphs)  # every paragraph reached some window
    assert out["decision"] == "accept_sorter"
    assert out["confidence"] == 0.7  # the least confident window
    assert out["coverage"]["windows_total"] == out["coverage"]["windows_checked"] == len(seen)


def test_windows_that_disagree_go_to_a_person(monkeypatch):
    text = "\n\n".join("Paragraph " + "word " * 300 for _ in range(10))
    replies = [{"decision": "accept_sorter", "confidence": 0.99, "reasoning": "a"},
               {"decision": "accept_reviewer", "confidence": 0.99, "reasoning": "b"}] * 5
    agent, _ = _agent_with(monkeypatch, replies, 4000)
    out = agent.arbitrate_classification(sorter={}, reviewer={}, causes=[], labels=["contract"], doc_text=text)
    assert out["decision"] == "human_review" and out["confidence"] is None


def test_a_human_review_window_stops_the_read_and_wins(monkeypatch):
    text = "\n\n".join("Paragraph " + "word " * 300 for _ in range(10))
    replies = [{"decision": "accept_sorter", "confidence": 0.99, "reasoning": "a"},
               {"decision": "human_review", "confidence": 0.1, "reasoning": "mixed"}] + [{}] * 10
    agent, seen = _agent_with(monkeypatch, replies, 4000)
    out = agent.arbitrate_classification(sorter={}, reviewer={}, causes=[], labels=["contract"], doc_text=text)
    assert out["decision"] == "human_review"
    assert len(seen) == 2 and out["coverage"]["windows_checked"] == 2 < out["coverage"]["windows_total"]


def test_a_failing_window_is_not_swallowed(monkeypatch):
    """A provider error in a later window propagates: the node's handler routes it to a person."""
    agent = arbiter_mod.ClassificationArbiterAgent.__new__(arbiter_mod.ClassificationArbiterAgent)
    calls = []

    def call(message, schema):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("provider down")
        return {"decision": "accept_sorter", "confidence": 0.9, "reasoning": "ok"}

    monkeypatch.setattr(agent, "_call_structured", call, raising=False)
    monkeypatch.setattr(agent, "_configured_max_input_chars", lambda: 4000, raising=False)
    text = "\n\n".join("Paragraph " + "word " * 300 for _ in range(10))
    with pytest.raises(RuntimeError):
        agent.arbitrate_classification(sorter={}, reviewer={}, causes=[], labels=["contract"], doc_text=text)


def test_a_document_that_fits_one_window_is_sent_whole(monkeypatch):
    agent, seen = _agent_with(monkeypatch, [{"decision": "human_review", "confidence": 0.1, "reasoning": "x"}], 100_000)
    full = "y" * 20_000  # over the old 16,000-character cap
    agent.arbitrate_classification(sorter={}, reviewer={}, causes=[], labels=["contract"], doc_text=full)
    assert len(seen) == 1 and full in seen[0] and "truncated" not in seen[0] and "WINDOW" not in seen[0]


@pytest.mark.parametrize("subclass", [None, "", "  "])
def test_contract_without_a_subtype_is_not_accepted(decision_config, monkeypatch, subclass):
    """The arbiter cannot send a contract to extraction with no subtype."""
    decision_config["by_class"]["contract"] = {"mode": "decision_only", "cutoff": 0.9, "arbiter_cutoff": 0.6}
    stub_arbiter(monkeypatch, {"decision": "accept_sorter", "confidence": 0.99, "reasoning": "MSA"})
    st = parked(doc_type="contract", doc_subclass=subclass, contract_subtype=None)
    updates = classification_arbiter_node(st)
    assert updates["classification_arbiter_decision"] == "human_review"
    assert after_classification_arbiter({**st, **updates}) == "human_review"


@pytest.mark.parametrize("cutoff", ["high", 10**400, float("nan"), 1.5, True])
def test_bad_arbiter_cutoff_parks_instead_of_raising(decision_config, monkeypatch, cutoff):
    """A misconfigured arbiter_cutoff sends the document to a person; the run is not aborted."""
    decision_config["by_class"]["insurance_claim"]["arbiter_cutoff"] = cutoff
    stub_arbiter(monkeypatch, {"decision": "accept_sorter", "confidence": 0.99, "reasoning": "FNOL"})
    updates = classification_arbiter_node(parked())
    assert updates["classification_arbiter_decision"] == "human_review"
    assert "cutoff n/a" in updates["escalation_reason"]


def test_accepted_arbitration_is_in_provenance_and_the_manifest(decision_config, monkeypatch):
    """The arbiter model is named when it decided the class, and the outcome is durable."""
    from graph.build_graph import _lane_b_manifest_fields, _resolved_models
    from schemas.manifest import DocumentManifest

    stub_arbiter(monkeypatch, {"decision": "accept_sorter", "confidence": 0.99, "reasoning": "FNOL form"})
    st = {**parked(), **classification_arbiter_node(parked())}
    assert "arbiter=" in _resolved_models(st)
    assert "arbiter=" not in _resolved_models(parked())
    fields = _lane_b_manifest_fields(st)
    manifest = DocumentManifest(doc_id="d1", matter_id="M", original_filename="f.txt", **fields)
    assert manifest.classification_arbiter_decision == "accept_sorter"
    assert manifest.classification_arbiter_confidence == 0.99
    assert manifest.classification_arbiter_reasoning == "FNOL form"


def test_untuned_class_under_decision_default_reaches_the_arbiter_after_transient_exhaustion(
        decision_config):
    """``default_mode: decision_only`` makes an untuned class a decision lane on the transient path too."""
    decision_config["default_mode"] = "decision_only"
    decision_config["by_class"].pop("contract", None)
    st = parked(doc_type="contract", transient_error=True, transient_retries_review_classify=99,
                reviewer_causes=[REVIEWER_UNAVAILABLE], review_route=None)
    assert after_review_classify(st) == "classification_arbiter"
