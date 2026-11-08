"""Threshold calibrator (scripts/calibrate_thresholds.py) against a PLANTED calibration curve.

The synthetic generator chooses P(correct | confidence) per class, so the answer is known.
Synthetic data validates the harness only; it says nothing about any real model.
"""

from __future__ import annotations

import pytest
import yaml

from pipeline import policy_replay as pr
from pipeline.config import CONFIG_PATH
from pipeline.synthetic_records import (
    SORTER_CURVES,
    generate_records,
    planted_cutoff,
    planted_set_precision,
)
from scripts import calibrate_thresholds as ct

TOL = 0.03  # |proposed - planted| on a 3,000-per-class corpus (observed max ~0.02 over 3 seeds)


@pytest.fixture(scope="module")
def corpus():
    return generate_records(3000, seed=1)


@pytest.fixture(scope="module")
def result(corpus):
    train, _ = pr.select_split(corpus, "train")
    return ct.calibrate(train, pr.current_policy())


def test_planted_cutoff_is_recovered_within_tolerance(result):
    calibrated = {c: e["high"] for c, e in result["classes"].items() if e["high"]["status"] == "calibrated"}
    assert len(calibrated) >= 3, {c: e["high"]["reason"] for c, e in result["classes"].items()}
    compared = 0
    for cls, ev in calibrated.items():
        planted = planted_cutoff(cls, ev["effective_target"], "sorter")
        if planted is not None:  # the curve may not reach a lucky high effective target at all
            compared += 1
            assert abs(ev["proposed"] - planted) <= TOL, (cls, ev["proposed"], planted)
        # soundness: the TRUE accuracy of what the cutoff accepts clears the requested target
        assert planted_set_precision(cls, ev["proposed"], "sorter") >= result["params"]["target"], cls
    assert compared >= 3


def test_target_is_derived_from_the_cost_ratio(corpus):
    train, _ = pr.select_split(corpus, "train")
    small = train[:600]
    assert ct.calibrate(small, pr.current_policy())["params"]["target"] == pytest.approx(0.95)
    assert ct.calibrate(small, pr.current_policy(), c_error=100.0)["params"]["target"] == pytest.approx(0.99)
    assert ct.calibrate(small, pr.current_policy(), c_human=2.0, c_error=10.0)["params"]["target"] == pytest.approx(0.8)


def test_undersampled_class_gets_no_cutoff_and_keeps_its_current_value():
    recs = generate_records({**{c: 2500 for c in SORTER_CURVES}, "correspondence": 80}, seed=2)
    train, _ = pr.select_split(recs, "train")
    base = pr.current_policy()
    res = ct.calibrate(train, base)
    for key in ("high", "judge_band_high", "low"):
        ev = res["classes"]["correspondence"][key]
        assert ev["status"] == "insufficient_evidence" and ev["proposed"] is None, (key, ev)
        assert ev["reason"]
    assert res["classes"]["correspondence"]["high"]["n"] < 80
    assert res["candidate_policy"]["by_class"]["correspondence"] == base["by_class"]["correspondence"]
    # a class with plenty of data in the same corpus is still calibrated
    assert any(
        e["high"]["status"] == "calibrated" for c, e in res["classes"].items() if c != "correspondence"
    )


def test_shortfall_and_unreachable_target_are_refused_by_the_gate():
    kw = dict(folds=5, seed=42, confidence=0.95, risk=0.02)
    perfect_100 = [(0.90 + i / 1000.0, True) for i in range(100)]
    ev = ct.cutoff_evidence(perfect_100, 0.95, **kw)
    assert ev["status"] == "insufficient_evidence" and ev["reason"].startswith("sample_shortfall")
    assert ev["required"] == 149 and ev["shortfall"] == 49 and ev["proposed"] is None
    perfect_200 = [(0.90 + i / 2000.0, True) for i in range(200)]
    ok = ct.cutoff_evidence(perfect_200, 0.95, **kw)
    assert ok["status"] == "calibrated" and ok["shortfall"] == 0 and 0.5 <= ok["proposed"] <= 0.999
    never = [(0.90 + i / 1000.0, False) for i in range(300)]
    ev = ct.cutoff_evidence(never, 0.95, **kw)
    assert ev["reason"].startswith("no_cutoff_reaches_target") and ev["proposed"] is None
    assert ct.cutoff_evidence([(0.9, True)], 0.95, **kw)["reason"] == "too_few_documents"


def test_proposals_are_clamped_and_rounded_up():
    kw = dict(folds=5, seed=1, confidence=0.95, risk=0.02)
    ev = ct.cutoff_evidence([(1.0, True)] * 400, 0.95, **kw)  # cannot be proposed above 0.999
    assert ev["status"] == "calibrated" and ev["proposed"] == 0.999 and ev["clamped"]
    ev = ct.cutoff_evidence([(0.9234 + i * 1e-5, True) for i in range(400)], 0.95, **kw)
    assert ev["proposed"] == 0.924  # ceil to 3 places: never looser than the evidence


def test_candidate_replays_and_only_calibrated_values_change(corpus, result):
    base = pr.current_policy()
    cand = result["candidate_policy"]
    for cls, entry in result["classes"].items():
        for key, ev in entry.items():
            got = (cand["by_class"].get(cls) or {}).get(key)
            want = ev["proposed"] if ev["status"] == "calibrated" else (base["by_class"].get(cls) or {}).get(key)
            assert got == want, (cls, key)
    test_split, _ = pr.select_split(corpus, "test")
    res = pr.evaluate(test_split, cand)
    assert res["outcomes"][pr.UNOBSERVED] == 0  # full-information records replay any candidate


def test_cli_writes_a_loadable_candidate_and_never_touches_the_taxonomy(tmp_path, capsys):
    before = CONFIG_PATH.read_bytes()
    recs = tmp_path / "r.jsonl"
    pr.dump_records(generate_records(900, seed=4), recs)
    out = tmp_path / "cand.yaml"
    code = ct.main(["--records", str(recs), "--out", str(out)])
    stdout = capsys.readouterr().out
    assert code == 0 and "SYNTHETIC" in stdout and "wrote" in stdout
    assert "insufficient_evidence" in stdout or "calibrated" in stdout
    assert "reviewer lane" in stdout
    doc = yaml.safe_load(out.read_text())
    assert set(doc) == {"confidence", "calibration"}
    assert doc["calibration"]["params"]["target"] == pytest.approx(0.95)
    assert doc["calibration"]["split"] == "train"
    loaded = pr.load_policy(out)
    assert loaded["retry_max"] == pr.current_policy()["retry_max"]
    assert CONFIG_PATH.read_bytes() == before


def test_cli_bad_input_and_gt_in_state_exclusion(tmp_path, capsys):
    assert ct.main(["--records", str(tmp_path / "nope.jsonl"), "--out", str(tmp_path / "o.yaml")]) == 1
    assert not (tmp_path / "o.yaml").exists()
    recs = generate_records(40, seed=6)
    tainted = [r.model_copy(update={"run": r.run.model_copy(update={"gt_in_state": True})}) for r in recs]
    path = tmp_path / "t.jsonl"
    pr.dump_records(tainted, path)
    capsys.readouterr()
    assert ct.main(["--records", str(path), "--out", str(tmp_path / "o.yaml")]) == 1  # nothing usable left
    assert not (tmp_path / "o.yaml").exists()
