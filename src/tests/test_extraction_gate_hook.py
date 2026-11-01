"""Opt-in extraction gate: the handoff carries the sorter's subclass, and the gate is advisory.

Network-free: the gate is a fake that records its handoff.
"""

import time

import pytest

from agents.extraction_gate import (
    extraction_gate_handoff,
    get_extraction_gate,
    register_extraction_gate,
)
from graph import build_graph as bg


@pytest.fixture(autouse=True)
def _no_gate_left_behind():
    """Every test starts and ends with no gate registered."""
    register_extraction_gate(None)
    yield
    register_extraction_gate(None)


def _state(**overrides):
    """A classified contract with the sorter's subclass."""
    state = {
        "doc_id": "d-gate",
        "doc_text": "MASTER SERVICES AGREEMENT between Acme Corp. and Beta LLC.",
        "doc_type": "contract",
        "doc_subclass": "service_agreement",
        "stage": "classify",
    }
    state.update(overrides)
    return state


def _fake_contracts(doc_text, pages=None, handoff_context=None):
    """A contracts specialist that always returns the same extraction."""
    return {"parties": ["Acme Corp.", "Beta LLC"], "governing_law": "NY", "confidence": 0.95,
            "_raw": "diagnostic"}


class Spy:
    """Fake gate that records the handoff it was given."""

    def __init__(self, answer=None, exc=None):
        """Answer with ``answer`` or raise ``exc``."""
        self.calls = []
        self.answer = answer if answer is not None else {
            "verdict": "accept", "key_field_score": 0.91, "field_support": {"parties": 0.95},
            "accept_probability": 0.8, "noise": "dropped"}
        self.exc = exc

    def __call__(self, doc_text, handoff):
        """Record the call; answer or raise."""
        self.calls.append((doc_text, handoff))
        if self.exc:
            raise self.exc
        return self.answer


def test_handoff_contract():
    """Underscore keys and confidence are dropped; doc_subclass only when given."""
    out = extraction_gate_handoff("contract", {"parties": ["A"], "_raw": "x", "confidence": 0.9}, "nda")
    assert out == {"doc_type": "contract", "fields": {"parties": ["A"]}, "doc_subclass": "nda"}
    assert "doc_subclass" not in extraction_gate_handoff("contract", {"parties": ["A"]}, None)
    assert "doc_subclass" not in extraction_gate_handoff("contract", {"parties": ["A"]}, "  ")
    assert extraction_gate_handoff("contract", {"_parse_error": True}) == {"doc_type": "contract", "available": False}


def test_no_gate_registered_changes_nothing(monkeypatch):
    """Without a gate, extract_node output has no extraction_gate key."""
    monkeypatch.setattr(bg, "_extract_contracts", _fake_contracts)
    assert get_extraction_gate() is None
    assert "extraction_gate" not in bg.extract_node(_state())


def test_gate_receives_the_sorter_subclass(monkeypatch):
    """The handoff carries the sorter's doc_subclass and the cleaned fields."""
    monkeypatch.setattr(bg, "_extract_contracts", _fake_contracts)
    spy = Spy()
    register_extraction_gate(spy)
    updates = bg.extract_node(_state())
    (doc_text, handoff), = spy.calls
    assert doc_text.startswith("MASTER SERVICES AGREEMENT")
    assert handoff["doc_type"] == "contract" and handoff["doc_subclass"] == "service_agreement"
    fields = handoff["fields"]  # the pipeline's own contract enrichment rides along
    assert fields["parties"] == ["Acme Corp.", "Beta LLC"] and fields["governing_law"] == "NY"
    assert not any(k.startswith("_") or k == "confidence" for k in fields)
    gate = updates["extraction_gate"]
    assert gate["doc_subclass"] == "service_agreement"
    assert gate["verdict"] == "accept" and gate["key_field_score"] == 0.91
    assert "noise" not in gate


def test_contract_subtype_stands_in_and_no_subclass_is_sent_as_absent(monkeypatch):
    """The contract subtype is the sorter's subclass when doc_subclass is unset; none means none."""
    monkeypatch.setattr(bg, "_extract_contracts", _fake_contracts)
    spy = Spy()
    register_extraction_gate(spy)
    bg.extract_node(_state(doc_subclass=None, contract_subtype="license_agreement"))
    assert spy.calls[-1][1]["doc_subclass"] == "license_agreement"
    updates = bg.extract_node(_state(doc_subclass=None))
    assert "doc_subclass" not in spy.calls[-1][1]
    assert updates["extraction_gate"]["doc_subclass"] is None


def test_gate_is_advisory_routing_unchanged(monkeypatch):
    """A retry_extraction verdict does not change the node's routing inputs."""
    monkeypatch.setattr(bg, "_extract_contracts", _fake_contracts)
    base = bg.extract_node(_state())
    register_extraction_gate(Spy({"verdict": "human_review", "key_field_score": 0.0}))
    gated = bg.extract_node(_state())
    assert gated["extraction_gate"]["verdict"] == "human_review"
    assert {k: v for k, v in gated.items() if k != "extraction_gate"} == base


def test_gate_failure_is_recorded_not_raised(monkeypatch):
    """A gate exception, a malformed answer and a timeout are recorded; extraction completes."""
    monkeypatch.setattr(bg, "_extract_contracts", _fake_contracts)
    register_extraction_gate(Spy(exc=RuntimeError("provider down")))
    updates = bg.extract_node(_state())
    assert updates["extracted_data"]["parties"] == ["Acme Corp.", "Beta LLC"]
    assert updates["extraction_gate"]["error"].startswith("RuntimeError")
    register_extraction_gate(lambda doc_text, handoff: "accept")
    assert "malformed" in bg.extract_node(_state())["extraction_gate"]["error"]
    from pipeline import limits

    monkeypatch.setattr(limits, "get_call_timeout_seconds", lambda: 0.05)
    register_extraction_gate(lambda doc_text, handoff: time.sleep(0.5) or {"verdict": "accept"})
    assert bg.extract_node(_state())["extraction_gate"]["error"].startswith("timeout")


def test_parse_error_is_not_sent(monkeypatch):
    """A parse-failed extraction is skipped, not reviewed."""
    monkeypatch.setattr(bg, "_extract_contracts", lambda *a, **k: {"_parse_error": True, "confidence": 0.0})
    spy = Spy()
    register_extraction_gate(spy)
    assert bg.extract_node(_state())["extraction_gate"] == {"doc_subclass": "service_agreement",
                                                            "skipped": "parse_error"}
    assert spy.calls == []


def test_retry_extract_is_reviewed_too(monkeypatch):
    """retry_extract_node sends the same handoff."""
    monkeypatch.setattr(bg, "_extract_contracts", _fake_contracts)
    spy = Spy()
    register_extraction_gate(spy)
    updates = bg.retry_extract_node(_state())
    assert spy.calls[0][1]["doc_subclass"] == "service_agreement"
    assert updates["extraction_gate"]["verdict"] == "accept"
