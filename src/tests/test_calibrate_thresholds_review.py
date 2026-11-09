"""Review-driven hardening of scripts/calibrate_thresholds.py (PR #203 threads T29, T50, T51)."""

from __future__ import annotations

import random

import pytest

from scripts import calibrate_thresholds as ct

# -- T29: a null class override falls back to the global value, like routing ---------------


def test_null_class_override_falls_back_to_the_global_value():
    policy = {"high": 0.97, "low": 0.88, "by_class": {"letter": {"high": None, "low": 0.9}}}
    assert ct.effective_value(policy, "letter", "high") == 0.97  # null override -> global
    assert ct.effective_value(policy, "letter", "low") == 0.9  # real override wins
    assert ct.effective_value(policy, "other", "low") == 0.88  # no class entry -> global


def test_effective_value_is_none_only_when_class_and_global_are_both_absent():
    policy = {"high": 0.97, "by_class": {"letter": {"judge_band_high": None}}}
    assert ct.effective_value(policy, "letter", "judge_band_high") is None
    assert ct.effective_value({"by_class": None}, "letter", "high") is None


def test_effective_value_matches_routing_for_null_overrides(monkeypatch):
    """The same lookup ``pipeline.config.get_confidence_thresholds`` performs."""
    import pipeline.config as cfg

    block = {"high": 0.97, "low": 0.88, "by_class": {"correspondence": {"high": None, "low": 0.9}}}
    monkeypatch.setattr(cfg, "load_config", lambda: {"confidence": block})
    routed = cfg.get_confidence_thresholds("correspondence")
    for key in ("high", "low"):
        assert ct.effective_value(block, "correspondence", key) == routed[key]


# -- T50: the held-out gate scores the cutoff as emitted ----------------------------------


def _pairs_needing_the_lower_clamp():
    """400 correct documents above 0.5, 20 wrong ones in 0.30-0.49: the raw cutoff is below the clamp."""
    rng = random.Random(0)
    good = [(0.5 + i * 0.00125, True) for i in range(400)]
    bad = [(0.30 + i * 0.009, False) for i in range(20)]
    rows = good + bad
    rng.shuffle(rows)
    return rows


def test_holdout_is_scored_on_the_clamped_cutoff_not_the_raw_one():
    from llm_dojo_scoring.analysis.calibration import cross_fit_cutoffs, cutoff_at_target

    pairs = _pairs_needing_the_lower_clamp()
    assert cutoff_at_target(pairs, 0.95) < ct.CLAMP_LO  # the raw tuned cutoff would be clamped up
    raw = cross_fit_cutoffs(pairs, target=0.95, folds=5, seed=42, confidence=0.95)
    deployed = ct.deployed_cross_fit(pairs, 0.95, folds=5, seed=42, confidence=0.95)
    assert raw["holdout_accuracy"] < 1.0  # the raw cutoff admits the wrong documents under 0.5
    assert deployed["holdout_accuracy"] == 1.0  # the emitted cutoff (0.5) does not
    ev = ct._evidence_at(pairs, 0.95, 0.95, folds=5, seed=42, confidence=0.95, risk=None, min_accepted=0)
    assert ev["status"] == "calibrated"
    assert ev["proposed"] == ct.CLAMP_LO
    assert ev["cross_fit"]["holdout_accuracy"] == 1.0
    assert "meets_target" not in ev["cross_fit"]  # the raw-cutoff verdict is not carried along


def test_holdout_is_scored_on_the_upper_clamp():
    """Raw cutoff 1.0 is emitted as 0.999: the held-out set must include the 0.999-1.0 documents."""
    from llm_dojo_scoring.analysis.calibration import cutoff_at_target

    pairs = [(1.0, True)] * 60 + [(0.9995, False)] * 40 + [(0.9, False)] * 20
    assert cutoff_at_target(pairs, 0.95) == 1.0
    deployed = ct.deployed_cross_fit(pairs, 0.95, folds=5, seed=1, confidence=0.95)
    assert deployed["holdout_accuracy"] < 0.95  # the 0.9995 errors are accepted at 0.999


def test_deployed_cross_fit_equals_the_dojo_when_nothing_is_clamped():
    from llm_dojo_scoring.analysis.calibration import cross_fit_cutoffs

    rng = random.Random(3)
    pairs = [(round(0.6 + i * 0.001, 3), rng.random() < 0.6 + i * 0.0009) for i in range(390)]
    raw = cross_fit_cutoffs(pairs, target=0.9, folds=5, seed=42, confidence=0.95)
    mine = ct.deployed_cross_fit(pairs, 0.9, folds=5, seed=42, confidence=0.95)
    for key in ("holdout_accepted", "holdout_accuracy", "holdout_accuracy_ci", "holdout_share_accepted"):
        assert mine[key] == raw[key], key


# -- T51: low stays below both upper cuts in the emitted policy ---------------------------


def _ev(status, proposed, current):
    return {"status": status, "proposed": proposed, "current": current, "reason": None}


def _entry(low, high, jbh):
    return {"low": _ev(*low), "high": _ev(*high), "judge_band_high": _ev(*jbh)}


def test_calibrated_low_at_or_above_the_sorter_high_is_demoted():
    entry = _entry(("calibrated", 0.96, 0.88), ("insufficient_evidence", None, 0.95), ("insufficient_evidence", None, 0.99))
    ct._reconcile_bands(entry)
    assert entry["low"]["status"] == "insufficient_evidence" and entry["low"]["proposed"] is None
    assert entry["low"]["reason"] == "low_not_below_high"


def test_calibrated_high_under_an_uncalibrated_low_is_demoted():
    entry = _entry(("insufficient_evidence", None, 0.90), ("calibrated", 0.85, 0.97), ("insufficient_evidence", None, 0.95))
    ct._reconcile_bands(entry)
    assert entry["high"]["status"] == "insufficient_evidence" and entry["high"]["reason"] == "high_not_above_low"
    assert entry["low"]["status"] == "insufficient_evidence"


def test_calibrated_judge_band_high_under_an_uncalibrated_low_is_demoted():
    entry = _entry(("insufficient_evidence", None, 0.90), ("insufficient_evidence", None, 0.97), ("calibrated", 0.85, 0.95))
    ct._reconcile_bands(entry)
    assert entry["judge_band_high"]["status"] == "insufficient_evidence"
    assert entry["judge_band_high"]["reason"] == "judge_band_high_not_above_low"


def test_demoting_low_re_checks_the_fallback_current_low():
    """Low demoted back to its current 0.95 still sits above the calibrated high 0.90: demote high too."""
    entry = _entry(("calibrated", 0.96, 0.95), ("calibrated", 0.90, 0.97), ("insufficient_evidence", None, 0.99))
    ct._reconcile_bands(entry)
    assert entry["low"]["status"] == "insufficient_evidence"
    assert entry["high"]["status"] == "insufficient_evidence"


def test_demoting_low_for_the_sorter_high_re_checks_the_extraction_band():
    """Low 0.80 clears the calibrated judge_band_high 0.90 but not the calibrated high 0.75: demoting it
    restores its current 0.95, which now sits above the already-accepted judge_band_high."""
    entry = _entry(("calibrated", 0.80, 0.95), ("calibrated", 0.75, 0.97), ("calibrated", 0.90, 0.99))
    ct._reconcile_bands(entry)
    assert entry["low"]["status"] == "insufficient_evidence"
    assert entry["low"]["reason"] == "low_not_below_high"
    in_force = {k: ct._in_force(v) for k, v in entry.items()}
    assert in_force["low"] < in_force["judge_band_high"]  # no empty judge-gate band is emitted
    assert in_force["low"] < in_force["high"]
    assert entry["judge_band_high"]["status"] == "insufficient_evidence"
    assert entry["judge_band_high"]["reason"] == "judge_band_high_not_above_low"
    assert entry["high"]["status"] == "insufficient_evidence"  # 0.75 sits under the restored 0.95 too


@pytest.mark.parametrize("low", [("calibrated", 0.80, 0.88), ("insufficient_evidence", None, 0.88)])
def test_a_consistent_band_is_left_alone(low):
    entry = _entry(low, ("calibrated", 0.97, 0.97), ("calibrated", 0.95, 0.95))
    ct._reconcile_bands(entry)
    assert entry["high"]["status"] == entry["judge_band_high"]["status"] == "calibrated"
    assert entry["low"]["status"] == low[0]


def test_an_inverted_base_policy_with_nothing_calibrated_is_not_touched():
    entry = _entry(("insufficient_evidence", None, 0.99), ("insufficient_evidence", None, 0.9), ("insufficient_evidence", None, 0.9))
    ct._reconcile_bands(entry)
    assert all(e["status"] == "insufficient_evidence" and e["reason"] is None for e in entry.values())
