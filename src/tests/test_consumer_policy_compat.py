"""Verify producer policies while executing the real canonical Dojo engine."""
from contextlib import contextmanager
import sys


@contextmanager
def _calls():
    """Observe function entries without replacing scorer implementations."""
    seen = set()
    previous = sys.getprofile()

    def record(frame, event, arg):
        """Record the module and name of actual executed functions."""
        if event == "call":
            seen.add((frame.f_globals.get("__name__"), frame.f_code.co_name))

    try:
        sys.setprofile(record)
        yield seen
    finally:
        sys.setprofile(previous)


def test_legalbench_uses_producer_groups_without_changing_global_settings(monkeypatch):
    """License/maintenance credit must survive an unrelated Dojo configuration."""
    from legalbench.scoring import score_multiclass
    from llm_dojo_scoring.config import get_settings
    from langchain_agents.sorter_agent import SUBTYPE_EQUIVALENCES

    settings = get_settings()
    monkeypatch.setattr(settings, "subtype_equivalences", [])
    before = list(SUBTYPE_EQUIVALENCES)
    with _calls() as seen:
        result = score_multiclass([{
            "status": "ok", "expected": "license", "predicted": "maintenance",
            "correct": False, "family": "contract",
        }])
    assert result["accuracy"] == 0.0
    assert result["accuracy_equiv"] == 1.0
    assert settings.subtype_equivalences == []
    assert SUBTYPE_EQUIVALENCES == before
    assert ("llm_dojo_scoring.legalbench_scoring", "score_multiclass") in seen


def test_raw_pilot_calibration_delegates_round_last_and_empty_support():
    """Raw .00049/.00049/.00149 errors round to .001 after aggregation."""
    from scripts.run_pilot import summarize

    rows = [{
        "stage": "review", "class_match": False,
        "expected_doc_class": "contract", "wall_time_s": 0.0,
        "llm_calls": 0, "classification_confidence": confidence,
    } for confidence in (.00049, .00049, .00149)]
    with _calls() as seen:
        result = summarize(rows)
        empty = summarize([])
    assert result["mean_calibration_error"] == .001
    assert result["calibration_n"] == 3
    assert empty["mean_calibration_error"] is None
    assert ("llm_dojo_scoring.pilot_summary", "pilot_calibration_error") in seen


def test_diagnostic_public_imports_reexport_canonical_objects():
    """Established metric helper imports remain available to external users."""
    from observability import metrics
    from llm_dojo_scoring import diagnostics

    for name in ("DATE_FIELDS", "DURATION_FIELDS", "parse_date", "parse_money", "is_entity_list"):
        assert getattr(metrics, name) is getattr(diagnostics, name)
