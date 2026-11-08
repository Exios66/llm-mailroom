"""Offline policy replay, the hill-climb objective and its guards (pipeline/policy_replay.py).

Records here are hand-built or synthetic; no LLM, network or taxonomy edit is involved.
"""

from __future__ import annotations

import pytest

import graph.routing as routing
import pipeline.config as pconfig
from pipeline import policy_replay as pr
from schemas.run_record import (
    ArbiterObservation,
    ExtractionAttempt,
    Gold,
    JudgeObservation,
    ReviewerObservation,
    RunInfo,
    RunRecord,
    SorterAttempt,
)

# Explicit, taxonomy-independent policy: classify high .95 / low .80, extraction
# auto-archive cut (judge_band_high) .90, retry_max 2.
POLICY = {
    "high": 0.95,
    "low": 0.80,
    "retry_max": 2,
    "judge_band_high": 0.90,
    "arbiter_retry_max": 2,
    "judge_max_passes": 3,
    "by_class": {},
}


def sorter(conf, attempt=1, doc_type="contract", cost=0.0):
    return SorterAttempt(attempt=attempt, doc_type=doc_type, confidence_raw=conf, confidence=conf, cost_usd=cost)


def extract(conf, attempt=1, score=0.9, cost=0.0, **kw):
    return ExtractionAttempt(
        attempt=attempt, confidence_raw=conf, confidence=conf, extraction_score=score, cost_usd=cost, **kw
    )


def record(doc_id="d", gold="contract", sorters=(), extractions=(), reviewer=None, judges=(), arbiters=()):
    judges = list(judges)
    return RunRecord(
        run=RunInfo(run_id="t", mode="synthetic", split="test"),
        doc_id=doc_id,
        gold=Gold(expected_doc_type=gold),
        sorter=list(sorters),
        extraction=list(extractions),
        reviewer=reviewer or ReviewerObservation(),
        judge=judges[0] if judges else JudgeObservation(),
        judges=judges[1:],
        arbiters=list(arbiters),
    )


def judge(verdict, attempt=1, verified=False, cost=0.0):
    return JudgeObservation(ran=True, on_attempt=attempt, verdict=verdict, source_verified=verified, cost_usd=cost)


def arbiter(decision, attempt=1, cost=0.0):
    return ArbiterObservation(ran=True, on_attempt=attempt, decision=decision, cost_usd=cost)


def reviewer(doc_type, conf, cost=0.0):
    return ReviewerObservation(
        ran=True, mode="llm", reviewer_doc_type=doc_type, reviewer_confidence=conf, cost_usd=cost
    )


# ---- (a) one path per terminal outcome ------------------------------------------------


def test_direct_auto_archive_path_and_cost():
    rec = record(sorters=[sorter(0.97, cost=0.01)], extractions=[extract(0.95, cost=0.02)])
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.AUTO
    assert out.path == ["classify", "extract", "compile_report", "catalog_write", "archive"]
    assert out.final_class == "contract"
    assert out.llm_cost_usd == pytest.approx(0.03)
    assert out.llm_calls == 2
    assert out.extraction_score == 0.9


def test_low_sorter_confidence_goes_to_human_after_the_one_retry():
    rec = record(sorters=[sorter(0.50), sorter(0.55, attempt=2)])
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.HUMAN
    assert out.path == ["classify", "retry_classify", "human_review"]


def test_medium_sorter_goes_through_retry_then_reviewer_and_is_accepted():
    rec = record(
        sorters=[sorter(0.85), sorter(0.86, attempt=2)],
        reviewer=reviewer("contract", 0.97, cost=0.005),
        extractions=[extract(0.95)],
    )
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.AUTO
    assert out.path[:4] == ["classify", "retry_classify", "review_classify", "extract"]
    assert out.llm_cost_usd == pytest.approx(0.005)


def test_reviewer_verdict_is_recomputed_under_the_candidate_policy():
    rec = record(
        sorters=[sorter(0.85), sorter(0.86, attempt=2)],
        reviewer=reviewer("contract", 0.96),
        extractions=[extract(0.95)],
    )
    assert pr.replay(rec, POLICY).outcome == pr.AUTO  # 0.96 >= high 0.95: agrees_high
    stricter = {**POLICY, "high": 0.99}
    out = pr.replay(rec, stricter)  # 0.96 < 0.99: the same record now parks
    assert out.outcome == pr.HUMAN
    assert out.path[-1] == "human_review"


def test_unobserved_when_the_policy_needs_a_retry_the_record_lacks():
    rec = record(sorters=[sorter(0.85)])  # medium band -> retry_classify, never run
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.UNOBSERVED
    assert out.reasons == ["sorter_retry_not_observed"]
    assert out.llm_calls == 1  # the step that did run is still charged


def test_unobserved_reviewer_judge_boss_and_missing_extraction():
    medium = [sorter(0.85), sorter(0.86, attempt=2)]
    assert pr.replay(record(sorters=medium), POLICY).reasons == ["reviewer_not_observed"]
    judged = record(sorters=[sorter(0.97)], extractions=[extract(0.85)])  # [low, band_high): judge
    assert pr.replay(judged, POLICY).reasons == ["judge_not_observed_on_attempt_1"]
    conflict = record(sorters=[sorter(0.97)], extractions=[extract(0.95, conflict_detected=True)])
    assert pr.replay(conflict, POLICY).reasons == ["boss_escalation_not_observed"]
    no_ext = record(sorters=[sorter(0.97)])
    assert pr.replay(no_ext, POLICY).reasons == ["extraction_attempt_1_not_observed"]
    transient = record(sorters=[sorter(0.97).model_copy(update={"transient_error": True})])
    assert pr.replay(transient, POLICY).reasons == ["sorter_attempt_1_transient_error"]


def test_extraction_retry_then_archive():
    rec = record(sorters=[sorter(0.97)], extractions=[extract(0.60, score=0.3), extract(0.95, attempt=2)])
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.AUTO
    assert out.path[1:4] == ["extract", "retry_extract", "compile_report"]
    assert out.extraction_attempt == 2 and out.extraction_score == 0.9


def test_hollow_and_schema_invalid_extractions_are_retried_not_archived():
    hollow = record(
        sorters=[sorter(0.97)], extractions=[extract(0.99, hollow=True), extract(0.95, attempt=2)]
    )
    assert pr.replay(hollow, POLICY).path[1:3] == ["extract", "retry_extract"]
    invalid = record(
        sorters=[sorter(0.97)], extractions=[extract(0.99, schema_valid=False), extract(0.95, attempt=2)]
    )
    out = pr.replay(invalid, POLICY)
    assert out.outcome == pr.AUTO and "retry_extract" in out.path


def test_spent_retries_with_low_confidence_park_for_a_person():
    rec = record(
        sorters=[sorter(0.97)],
        extractions=[extract(0.3), extract(0.3, attempt=2), extract(0.3, attempt=3)],
    )
    out = pr.replay(rec, POLICY)  # no judge observation: the judge branch is needed first
    assert out.outcome == pr.UNOBSERVED
    out = pr.replay(
        record(sorters=rec.sorter, extractions=rec.extraction, judges=[judge("partial", attempt=3)]), POLICY
    )
    assert out.reasons == ["arbiter_not_observed_on_attempt_3"]


def test_judge_complete_archives_and_partial_follows_the_arbiter():
    base = dict(sorters=[sorter(0.97)], extractions=[extract(0.85)])
    ok = pr.replay(record(**base, judges=[judge("complete", cost=0.015)]), POLICY)
    assert ok.outcome == pr.AUTO and "judge_verify" in ok.path
    assert ok.llm_cost_usd == pytest.approx(0.015)
    caveats = record(**base, judges=[judge("partial")], arbiters=[arbiter("accept_with_caveats")])
    assert pr.replay(caveats, POLICY).outcome == pr.AUTO
    giveup = record(**base, judges=[judge("incomplete")], arbiters=[arbiter("human_review")])
    assert pr.replay(giveup, POLICY).outcome == pr.HUMAN
    retry = record(
        sorters=[sorter(0.97)],
        extractions=[extract(0.85), extract(0.95, attempt=2)],
        judges=[judge("partial")],
        arbiters=[arbiter("retry_extraction")],
    )
    out = pr.replay(retry, POLICY)
    assert out.outcome == pr.AUTO
    assert out.path.count("retry_extract") == 1 and "arbiter" in out.path


def test_judge_observation_for_another_attempt_is_not_borrowed():
    rec = record(sorters=[sorter(0.97)], extractions=[extract(0.85)], judges=[judge("complete", attempt=2)])
    assert pr.replay(rec, POLICY).outcome == pr.UNOBSERVED


# ---- (b) replay really calls the routing functions ------------------------------------


def test_monkeypatching_a_routing_function_changes_the_outcome(monkeypatch):
    rec = record(sorters=[sorter(0.97)], extractions=[extract(0.95)])
    assert pr.replay(rec, POLICY).outcome == pr.AUTO
    monkeypatch.setattr(routing, "after_report", lambda state: "human_review")
    assert pr.replay(rec, POLICY).outcome == pr.HUMAN
    monkeypatch.undo()
    monkeypatch.setattr(routing, "after_classify", lambda state: "human_review")
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.HUMAN and out.path == ["classify", "human_review"]


def test_oracle_puts_the_gold_class_in_routing_state():
    # Sorter says correspondence at 0.99, gold is contract: production archives it,
    # the oracle replay (class_misses_ground_truth) sends it to the reviewer, which was not observed.
    rec = record(sorters=[sorter(0.99, doc_type="correspondence")], extractions=[extract(0.95)])
    assert pr.replay(rec, POLICY).outcome == pr.AUTO
    out = pr.replay(rec, POLICY, oracle=True)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["reviewer_not_observed"]


# ---- (c) policy_override ---------------------------------------------------------------


def test_policy_override_serves_the_candidate_and_restores(monkeypatch):
    original = pconfig.load_config
    before = pconfig.get_confidence_thresholds("contract")
    candidate = pr.merge_policy(pr.current_policy(), {"by_class": {"contract": {"high": 0.5, "low": 0.1}}})
    with pr.policy_override(candidate):
        seen = pconfig.get_confidence_thresholds("contract")
        assert seen["high"] == 0.5 and seen["low"] == 0.1
        pconfig.clear_config_cache()  # must not blow up on the patched loader
        assert pconfig.get_confidence_thresholds("contract")["high"] == 0.5
    assert pconfig.load_config is original
    assert pconfig.get_confidence_thresholds("contract") == before
    with pytest.raises(RuntimeError):
        with pr.policy_override(candidate):
            raise RuntimeError("boom")
    assert pconfig.load_config is original
    assert pconfig.get_confidence_thresholds("contract") == before


def test_load_policy_overlays_a_partial_file_on_the_current_taxonomy(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("confidence:\n  by_class:\n    contract:\n      high: 0.91\n")
    pol = pr.load_policy(f)
    assert pol["by_class"]["contract"]["high"] == 0.91
    assert pol["by_class"]["contract"]["low"] == pr.current_policy()["by_class"]["contract"]["low"]
    assert pol["retry_max"] == pr.current_policy()["retry_max"]


# ---- (d) objective arithmetic -----------------------------------------------------------


def _four_doc_set():
    return [
        # A: archived, class and fields right; $0.40 of LLM spend = 0.1 C_h
        record("A", sorters=[sorter(0.97, cost=0.40)], extractions=[extract(0.95, score=0.95)]),
        # B: archived with the right class but extraction_score 0.5 < floor: wrong auto
        record("B", sorters=[sorter(0.97)], extractions=[extract(0.95, score=0.5)]),
        # C: parked for a person
        record("C", sorters=[sorter(0.50), sorter(0.50, attempt=2)]),
        # D: needs a retry that was not run
        record("D", sorters=[sorter(0.85)]),
    ]


def test_objective_value_is_hand_computed_exactly():
    res = pr.evaluate(_four_doc_set(), POLICY)
    # costs in C_h units: A 0 + 0.40/4 | B 20 | C 1 | D 1
    assert res["per_doc_cost"] == {"A": pytest.approx(0.1), "B": 20.0, "C": 1.0, "D": 1.0}
    assert res["mean_cost_units"] == pytest.approx(22.1 / 4)
    assert res["score"] == pytest.approx(1 - 22.1 / 4)  # -4.525
    assert res["outcomes"] == {pr.AUTO: 2, pr.HUMAN: 1, pr.UNOBSERVED: 1}
    assert res["auto"]["correct"] == 1 and res["auto"]["wrong"] == 1
    assert res["stp_rate"] == 0.5 and res["review_rate"] == 0.25 and res["unobserved_rate"] == 0.25
    assert res["llm_usd_per_doc"] == pytest.approx(0.1)


def test_objective_extremes_and_parameters():
    all_human = [record(str(i), sorters=[sorter(0.1), sorter(0.1, attempt=2)]) for i in range(10)]
    assert pr.evaluate(all_human, POLICY)["score"] == pytest.approx(0.0)
    perfect = [record(str(i), sorters=[sorter(0.99)], extractions=[extract(0.99)]) for i in range(10)]
    assert pr.evaluate(perfect, POLICY)["score"] == pytest.approx(1.0)
    p = pr.ObjectiveParams(c_error=10.0, field_floor=0.95)
    assert p.accept_target == pytest.approx(0.9)
    assert pr.accept_target() == pytest.approx(0.95)
    wrong = [record("w", sorters=[sorter(0.99)], extractions=[extract(0.99, score=0.9)])]
    assert pr.evaluate(wrong, POLICY, p)["score"] == pytest.approx(1 - 10.0)  # score 0.9 < floor 0.95
    with pytest.raises(ValueError):
        pr.ObjectiveParams.from_mapping({"nope": 1})


def test_unknown_extraction_score_is_charged_as_wrong_and_reported_separately():
    unscored = record("u", sorters=[sorter(0.99)], extractions=[extract(0.99, score=None)])
    res = pr.evaluate([unscored], POLICY)
    assert res["auto"]["unscored"] == 1 and res["auto"]["wrong"] == 0
    assert res["score"] == pytest.approx(1 - 20.0)
    assert res["auto"]["wrong_rate"] == 1.0


def test_wrong_class_is_wrong_even_with_a_perfect_extraction():
    rec = record("x", gold="correspondence", sorters=[sorter(0.99)], extractions=[extract(0.99, score=1.0)])
    assert pr.evaluate([rec], POLICY)["auto"]["wrong"] == 1


# ---- (e) guards --------------------------------------------------------------------------


def _clean(n, prefix="c", **kw):
    return [record(f"{prefix}{i}", sorters=[sorter(0.99)], extractions=[extract(0.99)], **kw) for i in range(n)]


def _guards(res):
    return {g["id"]: g for g in res["guards"]}


def test_g1_needs_enough_clean_archives_to_bound_the_error_rate():
    small = pr.evaluate(_clean(50), POLICY)  # CP upper of 0/50 is 0.058 > 0.02
    assert not _guards(small)["G1"]["pass"] and small["status"] == "guard_fail"
    big = pr.evaluate(_clean(300), POLICY)  # CP upper of 0/300 is 0.00995
    assert _guards(big)["G1"]["pass"] and big["status"] == "ok"
    assert big["score"] == pytest.approx(1.0)


def test_g2_flags_a_class_over_its_wrong_rate_only_with_enough_archives():
    bad = [record(f"b{i}", sorters=[sorter(0.99)], extractions=[extract(0.99, score=0.1)]) for i in range(4)]
    res = pr.evaluate(_clean(300) + bad, POLICY)
    assert _guards(res)["G2"]["pass"]  # 4/304 = 1.3% <= 5%
    many_bad = [record(f"m{i}", sorters=[sorter(0.99)], extractions=[extract(0.99, score=0.1)]) for i in range(20)]
    res = pr.evaluate(_clean(300) + many_bad, POLICY)
    assert not _guards(res)["G2"]["pass"] and "contract" in _guards(res)["G2"]["value"]
    few = pr.evaluate(many_bad[:10], POLICY)  # 10 archives < 30: G2 does not apply
    assert _guards(few)["G2"]["pass"]


def test_g3_unobserved_share_and_g4_cost_budget():
    unobserved = [record(f"o{i}", sorters=[sorter(0.85)]) for i in range(5)]
    res = pr.evaluate(_clean(300) + unobserved, POLICY)
    assert not _guards(res)["G3"]["pass"]  # 5/305 = 1.6% > 1%
    ok = pr.evaluate(_clean(300) + unobserved[:2], POLICY)
    assert _guards(ok)["G3"]["pass"]  # 2/302 = 0.66%
    costly = [r.model_copy(update={"sorter": [sorter(0.99, cost=0.50)]}) for r in _clean(300)]
    off = pr.evaluate(costly, POLICY)
    assert _guards(off)["G4"].get("skipped") and off["status"] == "ok"
    over = pr.evaluate(costly, POLICY, {"cost_budget_usd": 0.25})
    assert not _guards(over)["G4"]["pass"] and over["status"] == "guard_fail"
    under = pr.evaluate(costly, POLICY, {"cost_budget_usd": 0.75})
    assert _guards(under)["G4"]["pass"]


def test_risk_parameter_moves_the_g1_bar():
    res = pr.evaluate(_clean(100), POLICY, {"risk": 0.05})  # CP upper of 0/100 is 0.0295
    assert _guards(res)["G1"]["pass"]


# ---- split, io, paired delta --------------------------------------------------------------


def test_select_split_and_roundtrip(tmp_path):
    recs = [r.model_copy(update={"run": r.run.model_copy(update={"split": s})}) for r, s in zip(_clean(4), ["train", "test", "test", "train"])]
    assert [r.doc_id for r in pr.select_split(recs, "test")[0]] == ["c1", "c2"]
    unsplit = [r.model_copy(update={"run": r.run.model_copy(update={"split": "unsplit"})}) for r in recs]
    picked, warnings = pr.select_split(unsplit, "test")
    assert len(picked) == 4 and "unsplit" in warnings[0]
    assert pr.select_split(recs, "all")[0] == recs
    path = tmp_path / "r.jsonl"
    assert pr.dump_records(recs, path) == 4
    assert [r.doc_id for r in pr.load_records(path)] == ["c0", "c1", "c2", "c3"]
    path.write_text(path.read_text() + "{not json}\n")
    with pytest.raises(ValueError, match=":5:"):
        pr.load_records(path)


def test_paired_delta_detects_a_real_improvement():
    recs = _clean(60) + [record(f"h{i}", sorters=[sorter(0.1), sorter(0.1, attempt=2)]) for i in range(60)]
    strict = {**POLICY, "high": 0.995}  # nothing archives: every clean doc goes to a person
    a = pr.evaluate(recs, POLICY)
    b = pr.evaluate(recs, strict)
    d = pr.paired_delta(a, b)
    assert d["delta"] == pytest.approx(a["score"] - b["score"], abs=1e-3)
    assert d["significant"] and d["n_paired"] == 120


def test_full_synthetic_corpus_replays_without_unobserved():
    from pipeline.synthetic_records import generate_records

    recs = generate_records(60, seed=3)
    res = pr.evaluate(recs, None)
    assert res["n_docs"] == 300 and res["outcomes"][pr.UNOBSERVED] == 0
    assert all(r.run.mode == "synthetic" and r.observed.full_information for r in recs)
    assert pr.result_for_json(res)["n_docs"] == 300


# ---- (g) the CLI ----------------------------------------------------------------------------


def _run_cli(argv, capsys):
    from scripts import hillclimb_eval

    try:
        code = hillclimb_eval.main([str(a) for a in argv])
    except SystemExit as exc:  # argparse errors exit 1 (bad input)
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


def _score_lines(stdout):
    return [line for line in stdout.splitlines() if line.startswith("SCORE:")]


def test_cli_prints_one_final_score_line_and_exits_zero_when_guards_pass(tmp_path, capsys):
    path = tmp_path / "clean.jsonl"
    pr.dump_records(_clean(400), path)
    code, out, _ = _run_cli(["--records", path, "--split", "test"], capsys)
    assert code == 0
    assert out.strip().splitlines()[-1] == "SCORE: 1.0000"
    assert len(_score_lines(out)) == 1
    for gid in ("G1", "G2", "G3"):
        assert f"GUARD {gid} PASS" in out
    assert "GUARD G4 SKIP" in out


def test_cli_exits_two_on_guard_failure_but_still_prints_the_score(tmp_path, capsys):
    import json

    from pipeline.synthetic_records import generate_records

    path = tmp_path / "syn.jsonl"
    pr.dump_records(generate_records(150, seed=5), path)
    code, out, _ = _run_cli(["--records", path, "--json", tmp_path / "o.json"], capsys)
    assert code == 2
    assert "GUARD G1 FAIL" in out and "SYNTHETIC" in out
    assert out.strip().splitlines()[-1].startswith("SCORE: ") and len(_score_lines(out)) == 1
    payload = json.loads((tmp_path / "o.json").read_text())
    assert payload["candidate"]["status"] == "guard_fail" and "per_doc_cost" not in payload["candidate"]


def test_cli_baseline_policy_reports_a_paired_delta(tmp_path, capsys):
    path = tmp_path / "mixed.jsonl"
    humans = [record(f"h{i}", sorters=[sorter(0.1), sorter(0.1, attempt=2)]) for i in range(40)]
    pr.dump_records(_clean(300) + humans, path)
    strict = tmp_path / "strict.yaml"  # nothing can reach 0.999: baseline sends everything to a person
    strict.write_text("confidence:\n  high: 0.999\n  by_class:\n    contract:\n      high: 0.999\n")
    code, out, _ = _run_cli(["--records", path, "--baseline-policy", strict], capsys)
    assert code in (0, 2)
    assert "DELTA U (candidate - baseline) = +" in out and "(significant;" in out
    assert "BASELINE GUARD G1" in out and len(_score_lines(out)) == 1


def test_cli_bad_input_exits_one_without_a_score(tmp_path, capsys):
    code, out, err = _run_cli(["--records", tmp_path / "missing.jsonl"], capsys)
    assert code == 1 and not _score_lines(out) and "error" in err
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    code, out, _ = _run_cli(["--records", bad], capsys)
    assert code == 1 and not _score_lines(out)
    ok = tmp_path / "ok.jsonl"
    pr.dump_records(_clean(3), ok)
    assert _run_cli(["--records", ok, "--c-error", "0.5"], capsys)[0] == 1  # c_error must exceed c_human
    assert _run_cli(["--records", ok, "--split", "bogus"], capsys)[0] == 1
    assert _run_cli(["--records", ok, "--split", "train"], capsys)[0] == 1  # no train records in the file


def test_cli_runs_as_a_script(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent
    path = tmp_path / "clean.jsonl"
    pr.dump_records(_clean(400), path)
    proc = subprocess.run(
        [sys.executable, str(src / "scripts" / "hillclimb_eval.py"), "--records", str(path)],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(src)}, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "SCORE: 1.0000"


def test_replay_uses_only_the_routed_class_extractions():
    """A sweep restarts attempt numbers per candidate class; replay must not mix them."""
    from pipeline.policy_replay import _att, _for_class
    from schemas.run_record import ExtractionAttempt

    items = [ExtractionAttempt(attempt=1, doc_type="correspondence", confidence=0.1),
             ExtractionAttempt(attempt=1, doc_type="contract", confidence=0.99)]
    assert _att(_for_class(items, "contract"), 1).confidence == 0.99
    assert _att(_for_class(items, "correspondence"), 1).confidence == 0.1
