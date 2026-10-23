"""Execute real source engines and prove compatibility facades delegate."""
import math
import sys
from contextlib import contextmanager
import pytest


@contextmanager
def calls_to(prefix):
    """Observe executed source functions without replacing any scored unit."""
    seen = set()
    old = sys.getprofile()
    def profile(frame, event, arg):
        """Collect actual source function call entries."""
        if event == "call" and frame.f_globals.get("__name__", "").startswith(prefix):
            seen.add(frame.f_code.co_name)
    sys.setprofile(profile)
    try:
        yield seen
    finally:
        sys.setprofile(old)


def test_exact_facade_identity_and_mismatched_cardinality():
    """The consumer must use Dojo and reject a missing prediction tail."""
    from observability import classification_scoring as c
    from llm_dojo_scoring import pipeline_scoring as engine
    assert c.classes_match is engine.classes_match
    assert c.score_exact_classification is engine.score_exact_classification
    assert c.score_exact_classification(["a", "a"], ["a", None])["exact_accuracy"] == 0.5


def test_exact_cardinality_refuses_tail_loss():
    """The pre-transfer facade silently returned a perfect score for a lost tail."""
    from observability.classification_scoring import score_exact_classification
    with pytest.raises(ValueError, match="expected length 2 != predicted length 1"):
        score_exact_classification(iter(["a", "b"]), iter(["a"]))


def test_legalbench_facade_identity_and_actual_engine_calls():
    """LegalBench remains producer-flag based; both engines execute in Dojo."""
    from legalbench import scoring as c
    from llm_dojo_scoring import legalbench_scoring as engine
    assert c.score_binary is engine.score_binary
    with calls_to("llm_dojo_scoring") as calls:
        score = c.score_binary([{"status": "ok", "expected": "yes", "predicted": "yes", "correct": False, "confidence": 0.5}])
    assert score["accuracy"] == 0.0 and score["calibration_error"] == 0.5
    assert {"score_binary", "raw_classification_stats", "expected_calibration_error"} <= calls
    with calls_to("llm_dojo_scoring") as calls:
        multi = c.score_multiclass([{"status": "ok", "expected": "license", "predicted": "maintenance", "correct": False}])
    assert multi["accuracy"] == 0.0 and multi["accuracy_equiv"] == 1.0
    assert "score_multiclass" in calls


def test_experiment_delegates_union_class_and_selected_stage_metrics():
    """One hit and one predicted-only class: macro F1=(2/3+0)/2."""
    from scripts.run_experiment import compute_metrics
    with calls_to("llm_dojo_scoring") as calls:
        result = compute_metrics([
            {"expected": "a", "got": "a", "has_stage": True, "stage_ok": True},
            {"expected": "a", "got": "b", "has_stage": False},
        ])
    assert result == {"class_accuracy": 0.5, "macro_class_accuracy": 0.5, "macro_f1": 0.3333, "stage_accuracy": 1.0}
    assert {"score_experiment_rows", "raw_classification_stats"} <= calls


def test_usage_reducer_delegates_six_legacy_keys_and_nonfinite_behavior():
    """Known zero cost counts; ignored empty usage rows and mean/total retained."""
    from legalbench.runner import _tokens_summary
    with calls_to("llm_dojo_scoring.cost") as calls:
        out = _tokens_summary([{}, None, {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "cost": 0.0}, {"total_tokens": 4, "cost": 2.0}, {"total_tokens": 1}])
    assert out == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 10, "cost_usd": 1.0, "cost_total_usd": 2.0, "rows_with_usage": 3}
    assert "tokens_summary" in calls
    # Cost basis was never an input to this legacy report; projection preserves that.
    assert _tokens_summary([{"cost": 1, "cost_basis": "busy_window"}, {"cost": 3}])["cost_usd"] == 2.0
    assert math.isnan(_tokens_summary([{"cost": float("nan")}])["cost_usd"])
    with pytest.raises(ValueError, match="NaN"):
        _tokens_summary([{"total_tokens": float("nan")}])


def test_agent_rubrics_delegate_without_mocking_scorers():
    """Reporter string presence and repeated-token behavior are unchanged."""
    from observability.agent_eval import score_case, token_overlap
    from llm_dojo_scoring import agent_rubrics as engine
    assert token_overlap is engine.token_set_f1
    with calls_to("llm_dojo_scoring.agent_rubrics") as calls:
        report = score_case("reporter", {"expected_fields": {"party": "A", "date": "B"}}, {"note": "party"})
        text = score_case("pdf_transcriber", {"text": "a c"}, {"markdown": "a a b"})
        boss = score_case("boss", {}, {"decision": "approved"})
        arbiter = score_case("arbiter", {}, {"decision": "invalid"})
    assert report["key_presence"] == 0.5
    assert text["token_overlap"] == 0.5
    assert boss["decision_valid"] == 1 and arbiter["decision_valid"] == 0
    assert {"token_set_f1", "key_presence", "decision_valid"} <= calls


def test_local_pack_diagnostic_facades_use_real_dojo():
    """A quality-population gate used by local packs must have one owner."""
    from observability import honest_gaps as c
    from llm_dojo_scoring import agent_rubrics as engine
    assert c.insurance_gt_is_homogeneous is engine.insurance_gt_is_homogeneous
    assert c.insurance_expected_set_is_homogeneous is engine.insurance_expected_set_is_homogeneous
    from observability.local_eval_packs import score_local_packs
    with calls_to("llm_dojo_scoring.agent_rubrics") as calls:
        out = score_local_packs()
    assert out["insurance_contrast"]["gt_homogeneity"] is False
    assert out["insurance_contrast"]["hub_cms_shaped"]["gt_homogeneity"] is True
    assert out["corporate_extraction"]["perfect_extract"]["kind"] == "scorer_self_check"
    assert "insurance_expected_set_is_homogeneous" in calls
