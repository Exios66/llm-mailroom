"""Threshold calibrator (scripts/calibrate_thresholds.py) against a PLANTED calibration curve.

The synthetic generator chooses P(correct | confidence) per class, so the answer is known.
Synthetic data validates the harness only; it says nothing about any real model.
"""

from __future__ import annotations

from collections import Counter

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


def test_a_null_by_class_base_policy_is_normalized_before_calibration(corpus):
    """Reproduced crash: null "by_class" survived setdefault and AttributeError'd at the first
    calibrated class — after the expensive pass, so no candidate was written at all. Routing
    tolerates null (pipeline/config.py reads it as {})."""
    train, _ = pr.select_split(corpus, "train")
    base = dict(pr.current_policy(), by_class=None)
    res = ct.calibrate(train, base)
    assert isinstance(res["candidate_policy"]["by_class"], dict)
    assert any(e["high"]["status"] == "calibrated" for e in res["classes"].values())


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
    assert "att = one pair per extraction ATTEMPT" in stdout and " att " in stdout
    doc = yaml.safe_load(out.read_text())
    audit = next(iter(doc["calibration"]["classes"].values()))
    assert (audit["high"]["unit"], audit["judge_band_high"]["unit"]) == ("documents", "attempts")
    assert audit["judge_band_high"]["n_docs"] <= audit["judge_band_high"]["n"]
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


# ---- T43: every scored extraction attempt is evidence, not just the final one ---------------


def _attempt_record(i, attempts):
    """A class-correct document whose extraction attempts are ``[(confidence, score), ...]``."""
    from schemas.run_record import ExtractionAttempt, Gold, RunInfo, RunRecord, SorterAttempt

    return RunRecord(
        run=RunInfo(run_id="t", mode="synthetic", split="train"),
        doc_id=f"d{i}",
        gold=Gold(expected_doc_type="contract"),
        sorter=[SorterAttempt(attempt=1, doc_type="contract", confidence_raw=0.97, confidence=0.97)],
        extraction=[
            ExtractionAttempt(attempt=n, doc_type="contract", confidence_raw=c, confidence=c, extraction_score=s)
            for n, (c, s) in enumerate(attempts, 1)
        ],
    )


def _confidently_wrong_first_attempt_corpus():
    """400 documents. 100 of them: attempt 1 confidently WRONG (0.95-0.99), the retry right.

    Routing accepts any attempt above the cutoff, so attempt 1 would have been archived wrong.
    """
    recs = []
    for i in range(300):  # attempt 1 right at 0.90-0.99; the retry is right too
        c = 0.90 + (i % 10) / 100.0
        recs.append(_attempt_record(i, [(c, 0.95), (c, 0.95)]))
    for i in range(300, 400):
        c = 0.95 + (i % 5) / 100.0
        recs.append(_attempt_record(i, [(c, 0.20), (c, 0.95)]))
    return recs


def test_extraction_cutoff_is_not_earned_by_the_final_attempt_alone():
    recs = _confidently_wrong_first_attempt_corpus()
    base = {"high": 0.95, "low": 0.80, "judge_band_high": 0.90, "retry_max": 2, "by_class": {}}
    kw = dict(folds=5, seed=42, confidence=0.95, risk=0.02)
    # control: the final attempt alone is flawless, so the old evidence WOULD call the cutoff earned
    final_only = [(a.confidence, a.extraction_score >= 0.80) for r in recs for a in r.extraction[-1:]]
    assert ct.cutoff_evidence(final_only, 0.95, **kw)["status"] == "calibrated"
    res = ct.calibrate(recs, base)
    jbh = res["classes"]["contract"]["judge_band_high"]
    assert jbh["status"] == "insufficient_evidence" and jbh["proposed"] is None, jbh
    assert res["candidate_policy"]["by_class"].get("contract", {}).get("judge_band_high") is None
    low = res["classes"]["contract"]["low"]
    assert low["status"] == "insufficient_evidence", low
    # the counts say what they count: 800 attempts of 400 documents
    assert (jbh["unit"], jbh["n"], jbh["n_docs"]) == ("attempts", 800, 400)
    assert jbh["n_accepted_docs"] <= jbh["n_accepted"]


def test_sorter_evidence_is_counted_in_documents():
    recs = [_attempt_record(i, [(0.9, 0.9)]) for i in range(10)]
    res = ct.calibrate(recs, {"high": 0.95, "low": 0.8, "judge_band_high": 0.9, "by_class": {}})
    high = res["classes"]["contract"]["high"]
    assert (high["unit"], high["n"], high["n_docs"]) == ("documents", 10, 10)


def test_too_few_attempts_is_named_in_attempts():
    kw = dict(folds=5, seed=42, confidence=0.95, risk=0.02)
    assert ct.cutoff_evidence([(0.9, True)], 0.95, unit="attempts", **kw)["reason"] == "too_few_attempts"


def test_sample_size_rule_counts_documents_when_a_pair_is_an_attempt():
    """40 flawless documents x 5 attempts = 200 pairs, which would clear required_samples(0) = 149 if
    attempts were independent samples. Only 40 documents stand behind them: still a shortfall."""
    pairs = [(0.90 + (n * 7 + i) % 10 / 100.0, True) for i in range(40) for n in range(5)]
    docs = [i for i in range(40) for _ in range(5)]
    kw = dict(folds=5, seed=42, confidence=0.95, risk=0.02, target_step=0.0)
    as_documents = ct.cutoff_evidence(pairs, 0.5, **kw)  # pretends 200 independent documents
    assert as_documents["status"] == "calibrated"
    ev = ct.cutoff_evidence(pairs, 0.5, unit="attempts", docs=docs, **kw)
    assert ev["status"] == "insufficient_evidence" and ev["reason"] == "sample_shortfall", ev
    assert ev["n"] == 200 and ev["n_docs"] == 40 and ev["n_accepted_docs"] <= 40
    assert ev["required"] == 149 and ev["shortfall"] == 149 - ev["n_accepted_docs"]


def test_document_grouped_cross_fit_judges_a_document_by_its_worst_accepted_attempt():
    # 30 documents x 5 attempts at the same confidence, one wrong attempt each: 80% right per attempt,
    # but every document has a wrong attempt routing could have archived.
    pairs, docs = [], []
    for d in range(30):
        for n in range(5):
            pairs.append((0.95, n != 0))
            docs.append(d)
    by_attempt = ct.deployed_cross_fit(pairs, 0.5, folds=5, seed=1, confidence=0.95)
    by_doc = ct.deployed_cross_fit(pairs, 0.5, folds=5, seed=1, confidence=0.95, docs=docs)
    assert by_attempt["holdout_accuracy"] == pytest.approx(0.8)
    assert by_doc["holdout_accuracy"] == 0.0 and by_doc["holdout_accepted"] == 30  # documents, not attempts


def test_document_grouped_cross_fit_keeps_every_attempt_of_a_document_in_one_fold(monkeypatch):
    """Each fold's training rows hold ALL attempts of a document or none, and each document is held
    out exactly once: no document's attempts leak between training and holdout."""
    from llm_dojo_scoring.analysis import calibration

    n_docs, n_attempts = 20, 4
    pairs, docs, owner = [], [], {}
    for d in range(n_docs):
        for n in range(n_attempts):
            conf = 0.80 + (d * n_attempts + n) * 0.0002  # unique per attempt, so a row names its document
            pairs.append((conf, n != 0))
            docs.append(d)
            owner[conf] = d
    seen_training = []
    real = calibration.cutoff_at_target

    def spy(rows, target, *args, **kwargs):
        seen_training.append(list(rows))
        return real(rows, target, *args, **kwargs)

    monkeypatch.setattr(calibration, "cutoff_at_target", spy)
    ct.deployed_cross_fit(pairs, 0.5, folds=5, seed=3, confidence=0.95, docs=docs)
    assert len(seen_training) == 5
    held_out = Counter()
    for training in seen_training:
        per_doc = Counter(owner[conf] for conf, _ in training)
        assert set(per_doc.values()) == {n_attempts}  # a training document is whole, never split
        for d in set(range(n_docs)) - set(per_doc):
            held_out[d] += 1
    assert held_out == Counter({d: 1 for d in range(n_docs)})  # every document held out in exactly one fold
