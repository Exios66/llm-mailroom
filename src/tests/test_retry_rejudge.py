"""Regression tests for #241: a confident retry extraction must be re-judged.

``after_retry_extraction_gated`` used to reuse ``judge_gate`` (the ambiguous
band only), so a retry that self-reported 1.0 after an arbiter-ordered retry
archived without a verdict. ``retry_rejudge_due`` closes that gap, and
``judge_should_run`` shares it so ``judge_verify_node`` does not skip the pass
the router just asked for.
"""

from __future__ import annotations

import pytest

import graph.routing as routing
from graph.routing import after_extraction_gated, after_retry_extraction_gated, judge_should_run


def retry_rejudge_due(state):
    # Looked up at call time so these tests fail by assertion, not at import, on code without the predicate.
    return routing.retry_rejudge_due(state)


def _retry_state(**overrides):
    """Attempt 2, self-reported 1.0, attempt 1 was judged once."""
    state = {
        "doc_id": "d1",
        "doc_type": "correspondence",
        "extraction_confidence": 1.0,
        "extraction_attempts": 2,
        "judge_pass_count": 1,
        "extracted_data": {"sender": "Acme", "urgency": "routine"},
    }
    state.update(overrides)
    return state


def _patch_thresholds(monkeypatch, by_doc_type: dict, default: dict):
    monkeypatch.setattr(
        routing,
        "get_confidence_thresholds",
        lambda doc_type=None: dict(by_doc_type.get(doc_type, default)),
    )


@pytest.fixture(autouse=True)
def _knob_on(request, monkeypatch):
    """Most tests exercise the opt-in path; the default-off tests opt out of this fixture."""
    if request.node.name.startswith("test_shipped_"):
        return
    real = routing.get_confidence_thresholds
    monkeypatch.setattr(
        routing,
        "get_confidence_thresholds",
        lambda doc_type=None: {**real(doc_type), "judge_rejudge_retry": True},
    )


def test_confident_retry_after_a_judged_attempt_goes_back_to_the_judge():
    state = _retry_state()
    assert after_retry_extraction_gated(state) == "judge_verify"
    assert judge_should_run(state) is True
    assert retry_rejudge_due(state) is True


def test_exhausted_passes_keep_archiving():
    # judge_max_passes defaults to 3: no further verdict is owed, nothing routes to a person.
    state = _retry_state(judge_pass_count=3, extraction_attempts=4)
    assert retry_rejudge_due(state) is False
    assert judge_should_run(state) is False
    assert after_retry_extraction_gated(state) == "compile_report"


def test_max_passes_is_read_from_thresholds(monkeypatch):
    _patch_thresholds(monkeypatch, {}, {"low": 0.88, "judge_band_high": 0.95, "judge_max_passes": 1})
    state = _retry_state(judge_pass_count=1)
    assert retry_rejudge_due(state) is False
    assert after_retry_extraction_gated(state) == "compile_report"


def test_retry_never_judged_before_is_not_rejudged():
    # Attempt 1 failed on confidence alone and skipped the judge: nothing to "re-" judge.
    state = _retry_state(judge_pass_count=0)
    assert retry_rejudge_due(state) is False
    assert after_retry_extraction_gated(state) == "compile_report"


@pytest.mark.parametrize("knob", [False, 0])
def test_global_knob_off_restores_old_behavior(monkeypatch, knob):
    _patch_thresholds(
        monkeypatch, {}, {"low": 0.88, "judge_band_high": 0.95, "judge_max_passes": 3, "judge_rejudge_retry": knob}
    )
    state = _retry_state()
    assert retry_rejudge_due(state) is False
    assert judge_should_run(state) is False
    assert after_retry_extraction_gated(state) == "compile_report"


def test_per_class_knob_override(monkeypatch):
    base = {"low": 0.88, "judge_band_high": 0.95, "judge_max_passes": 3, "judge_rejudge_retry": True}
    _patch_thresholds(monkeypatch, {"contract": {**base, "judge_rejudge_retry": False}}, base)
    assert after_retry_extraction_gated(_retry_state(doc_type="contract")) == "compile_report"
    assert after_retry_extraction_gated(_retry_state(doc_type="correspondence")) == "judge_verify"


def test_absent_knob_defaults_to_off(monkeypatch):
    _patch_thresholds(monkeypatch, {}, {"low": 0.88, "judge_band_high": 0.95})
    assert retry_rejudge_due(_retry_state()) is False
    assert after_retry_extraction_gated(_retry_state()) == "compile_report"


def test_shipped_taxonomy_keeps_the_rejudge_off():
    """Until #242 calibrates the judge, a confident retry archives unjudged as before."""
    state = _retry_state()
    assert retry_rejudge_due(state) is False
    assert judge_should_run(state) is False
    assert after_retry_extraction_gated(state) == "compile_report"


def test_attempt_one_is_unaffected():
    # First attempt, even with a stale pass count, keeps the band-only gate.
    state = _retry_state(extraction_attempts=1, judge_pass_count=1)
    assert retry_rejudge_due(state) is False
    assert judge_should_run(state) is False
    assert after_extraction_gated(state) == "compile_report"
    assert after_extraction_gated(_retry_state(extraction_attempts=1, judge_pass_count=0)) == "compile_report"


def test_ambiguous_band_retry_still_engages_the_existing_gate():
    state = _retry_state(extraction_confidence=0.90, judge_pass_count=0)
    assert after_retry_extraction_gated(state) == "judge_verify"


def test_kill_switch_env_beats_rejudge(monkeypatch):
    monkeypatch.setenv("MAILROOM_JUDGE_VERIFY", "off")
    state = _retry_state()
    assert retry_rejudge_due(state) is False
    assert judge_should_run(state) is False
    assert after_retry_extraction_gated(state) == "compile_report"


def test_guardrail_or_empty_extraction_is_not_rejudged():
    # Same hard evidence that stops judge_should_run clearing an unsure extraction.
    assert retry_rejudge_due(_retry_state(extracted_data={})) is False
    assert retry_rejudge_due(_retry_state(extraction_guardrail=["bad"])) is False


def test_other_destinations_are_untouched():
    # A conflict still goes to the boss, a transient blip still self-loops.
    assert after_retry_extraction_gated(_retry_state(conflict_detected=True)) == "boss_escalation"
    transient = _retry_state(transient_error=True, transient_retries_retry_extract=0)
    assert after_retry_extraction_gated(transient) == "retry_extract"


def test_rejudge_is_logged_with_its_own_event(monkeypatch):
    from unittest.mock import MagicMock

    # A stub logger: structlog's global config is shared and order-dependent in the full suite.
    stub = MagicMock()
    monkeypatch.setattr(routing, "logger", stub)
    assert after_retry_extraction_gated(_retry_state()) == "judge_verify"
    events = [call.args[0] for call in stub.info.call_args_list]
    assert "judge_rejudge_retry" in events
    assert "judge_gate_engaged" not in events


def test_judge_verify_node_runs_for_a_confident_retry(monkeypatch):
    """The node's own ``judge_should_run`` guard must agree with the router."""
    from agents.judge import CompletenessJudge
    from graph.build_graph import judge_verify_node

    calls = []

    def completeness(self, doc_type, extracted, doc_text):
        calls.append(("completeness", sorted(extracted)))
        return {"completeness": 0.6, "completeness_label": "partial", "reasoning": "gap"}

    monkeypatch.setattr(CompletenessJudge, "judge_completeness", completeness)
    updates = judge_verify_node(_retry_state(doc_text="Dear Sir"))
    assert updates["judge_verdict"] == "partial"
    assert updates["judge_pass_count"] == 2
    assert calls

    # Passes spent: the node skips, as the router archives.
    skipped = judge_verify_node(_retry_state(doc_text="Dear Sir", judge_pass_count=3))
    assert skipped["judge_verdict"] == "skipped"
