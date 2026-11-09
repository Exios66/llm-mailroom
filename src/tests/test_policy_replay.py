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


def reviewer(doc_type, conf, cost=0.0, subclass=None):
    return ReviewerObservation(
        ran=True, mode="llm", reviewer_doc_type=doc_type, reviewer_doc_subclass=subclass,
        reviewer_confidence=conf, cost_usd=cost,
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


def test_replay_routes_with_the_extraction_of_the_routed_class_when_attempt_numbers_collide():
    """Two candidate classes both have an attempt 1: replay must score the routed class's one."""
    rec = record(
        sorters=[sorter(0.97)],
        extractions=[
            extract(0.10, doc_type="correspondence", score=0.1),  # would park if it were used
            extract(0.95, doc_type="contract", score=0.9),
        ],
    )
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.AUTO
    assert out.extraction_score == 0.9 and out.extraction_attempt == 1
    flipped = pr.replay(
        record(
            sorters=[sorter(0.97, doc_type="correspondence")],
            extractions=rec.extraction,
            gold="correspondence",
        ),
        POLICY,
    )
    assert flipped.extraction_score == 0.1  # the other class's attempt 1, not the contract one


# ---- review fixes: replay accounting ---------------------------------------------------------


def test_failed_chunk_extraction_is_capped_below_the_candidates_own_low():
    """A lost window caps live confidence below the policy's ``low``; replay must apply the
    candidate's cap, not trust the cap recorded under the sweep's thresholds."""
    rec = record(
        sorters=[sorter(0.97)],
        extractions=[extract(0.79, failed_chunks=1)],  # recorded under POLICY: low 0.80 - 0.01
    )
    lenient = {**POLICY, "low": 0.5, "judge_band_high": 0.6}
    out = pr.replay(rec, lenient)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["extraction_attempt_2_not_observed"]
    clean = record(sorters=[sorter(0.97)], extractions=[extract(0.79)])  # no lost window: unchanged
    assert pr.replay(clean, lenient).outcome == pr.AUTO


def _three_low_attempts(failed_chunks, judges=()):
    third = extract(0.40, attempt=3)
    if failed_chunks:
        third = third.model_copy(update={"failed_chunks": failed_chunks})
    return record("w", sorters=[sorter(0.97)],
                  extractions=[extract(0.40), extract(0.40, attempt=2), third], judges=judges)


def test_lost_chunks_are_not_cleared_by_a_complete_judge_verdict():
    """A lost window is hard evidence of missing data, so routing must reach the judge only on
    an intact payload: a recorded complete, source-verified verdict may not archive a partial
    extraction the live graph retried."""
    judged = _three_low_attempts(1, judges=[judge("complete", attempt=3, verified=True)])
    out = pr.replay(judged, POLICY)
    assert out.outcome == pr.HUMAN and out.path[-1] == "human_review"
    # the same route with the windows intact is exactly what the judge verdict is worth
    assert pr.replay(_three_low_attempts(0, judges=[judge("complete", attempt=3, verified=True)]),
                     POLICY).outcome == pr.AUTO


def test_lost_chunks_on_the_last_attempt_park_not_unobserved():
    """The recorded sweep ran no judge on a lost-window attempt, so replay parks the document
    (what the live graph did) instead of demanding an observation it can never have."""
    out = pr.replay(_three_low_attempts(2), POLICY)
    assert out.outcome == pr.HUMAN and out.path[-1] == "human_review"
    # parity control: without the lost window the route still needs the judge the record lacks
    assert pr.replay(_three_low_attempts(0), POLICY).outcome == pr.UNOBSERVED


def test_recorded_intake_cost_is_charged_once_per_document():
    rec = record("i", sorters=[sorter(0.97, cost=0.40)], extractions=[extract(0.95)])
    rec = rec.model_copy(update={"diagnostics": {"intake": {"llm_prep": True, "cost_usd": 0.40}}})
    out = pr.replay(rec, POLICY)
    assert out.llm_cost_usd == pytest.approx(0.80)
    res = pr.evaluate([rec], POLICY)
    assert res["llm_usd_per_doc"] == pytest.approx(0.80)
    assert res["per_doc_cost"]["i"] == pytest.approx(0.80 / 4)
    bad = rec.model_copy(update={"diagnostics": {"intake": {"cost_usd": float("nan")}}})
    assert pr.replay(bad, POLICY).llm_cost_usd == pytest.approx(0.40)  # malformed cost is ignored


def test_auto_archiving_a_human_rejected_document_is_wrong():
    rec = record("r", sorters=[sorter(0.99)], extractions=[extract(0.99, score=1.0)])
    rec = rec.model_copy(update={"gold": rec.gold.model_copy(update={"human_decision": "rejected"})})
    res = pr.evaluate([rec], POLICY)
    assert res["auto"]["wrong"] == 1 and res["auto"]["correct"] == 0
    assert res["score"] == pytest.approx(1 - 20.0)
    approved = rec.model_copy(update={"gold": rec.gold.model_copy(update={"human_decision": "approved"})})
    assert pr.evaluate([approved], POLICY)["auto"]["correct"] == 1


def test_repeated_doc_ids_keep_one_cost_per_scored_row_and_stay_paired():
    a = record("dup", sorters=[sorter(0.99)], extractions=[extract(0.99)])
    b = record("dup", sorters=[sorter(0.99)], extractions=[extract(0.99, score=0.1)])  # wrong auto
    res = pr.evaluate([a, b], POLICY)
    assert len(res["per_doc_cost"]) == 2
    assert sum(res["per_doc_cost"].values()) == pytest.approx(res["mean_cost_units"] * 2)
    strict = pr.evaluate([a, b], {**POLICY, "high": 0.995})
    d = pr.paired_delta(res, strict)
    assert d is not None and d["n_paired"] == 2
    # a real doc_id that itself ends in "#2" must not be overwritten by the generated key of
    # the repeated "dup": three rows stay three keys and keep their pairing.
    c = record("dup#2", sorters=[sorter(0.99)], extractions=[extract(0.99, score=0.1)])
    res3 = pr.evaluate([a, b, c], POLICY)
    assert sorted(res3["per_doc_cost"]) == ["dup", "dup#2", "dup#2#2"]
    assert sum(res3["per_doc_cost"].values()) == pytest.approx(res3["mean_cost_units"] * 3)
    strict3 = pr.evaluate([a, b, c], {**POLICY, "high": 0.995})
    d3 = pr.paired_delta(res3, strict3)
    assert d3 is not None and d3["n_paired"] == 3


def test_non_positive_or_non_finite_review_cost_is_rejected():
    rec = [record("x", sorters=[sorter(0.99)], extractions=[extract(0.99)])]
    for bad in (0.0, -4.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="review_cost_usd"):
            pr.evaluate(rec, POLICY, {"review_cost_usd": bad})


def test_llm_call_count_is_labelled_a_lower_bound():
    res = pr.evaluate([record("x", sorters=[sorter(0.99)], extractions=[extract(0.99)])], POLICY)
    assert res["llm_calls_basis"].startswith("lower_bound")


def test_extraction_pairs_ignore_other_candidate_classes():
    """The final scored attempt must come from the gold class, not a later attempt of another class."""
    rec = record(
        "p",
        sorters=[sorter(0.97)],
        extractions=[
            extract(0.90, doc_type="contract", score=0.9),
            extract(0.30, attempt=2, doc_type="correspondence", score=0.1),
        ],
    )
    assert pr.extraction_pairs([rec]) == {"contract": [(0.90, True)]}
    untyped = record("q", sorters=[sorter(0.97)], extractions=[extract(0.5, score=0.1)])
    assert pr.extraction_pairs([untyped]) == {"contract": [(0.5, False)]}  # older, untyped rows still count


# ---- review fixes: RunRecord -> ReviewRecord, synthetic generator -----------------------------


def test_to_review_record_uses_sorter_attempt_one_whatever_the_order():
    from schemas.run_record import to_review_record

    rec = record(sorters=[sorter(0.60, attempt=2, doc_type="correspondence"), sorter(0.97)])
    row = to_review_record(rec)
    assert row["sorter_doc_type"] == "contract" and row["sorter_confidence"] == 0.97
    assert to_review_record(record(sorters=[sorter(0.6, attempt=2)]))["sorter_doc_type"] is None


def test_to_review_record_reports_reviewer_usage_only_when_the_reviewer_ran():
    from schemas.run_record import to_review_record

    not_ran = to_review_record(record(sorters=[sorter(0.97)]))
    assert not_ran["cost_usd"] is None and not_ran["prompt_tokens"] is None
    assert not_ran["completion_tokens"] is None
    ran = ReviewerObservation(
        ran=True, mode="llm", reviewer_doc_type="contract", reviewer_confidence=0.9,
        cost_usd=0.0, prompt_tokens=10, completion_tokens=2,
    )
    row = to_review_record(record(sorters=[sorter(0.97)], reviewer=ran))
    assert row["cost_usd"] == 0.0 and row["prompt_tokens"] == 10 and row["completion_tokens"] == 2


def test_generate_records_rejects_a_single_distinct_class_before_drawing():
    from pipeline.synthetic_records import generate_records

    for classes in (["contract"], ["contract", "contract"]):
        with pytest.raises(ValueError, match="two distinct classes"):
            generate_records(5, classes=classes)
    assert len(generate_records(2, seed=1, classes=["contract", "correspondence"])) == 4


def test_route_with_an_unextracted_subclass_is_unobserved():
    """A collapsed candidate of the same class but another subclass was never
    extracted: replay must not reuse the first candidate's extraction for it."""
    collapsed = [{"doc_type": "contract", "subclass_differs": True,
                  "doc_subclass": "lease", "extracted_doc_subclass": "nda"}]
    rec = record(sorters=[SorterAttempt(attempt=1, doc_type="contract", doc_subclass="lease",
                                        confidence_raw=0.97, confidence=0.97)],
                 extractions=[extract(0.95)])
    rec.diagnostics["extraction_candidates_collapsed"] = collapsed
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["extraction_subclass_not_observed"]
    # the subclass that WAS extracted still replays normally
    rec.sorter[0].doc_subclass = "nda"
    assert pr.replay(rec, POLICY).outcome == pr.AUTO


def _reviewer_rec(subclass, extracted, reviewer_subclass, conf=0.85):
    """A sorter->reviewer record whose routed class was extracted under ``extracted``."""
    sorters = [
        SorterAttempt(attempt=1, doc_type="contract", doc_subclass=subclass,
                      confidence_raw=conf, confidence=conf),
        SorterAttempt(attempt=2, doc_type="contract", doc_subclass=subclass,
                      confidence_raw=0.86, confidence=0.86),
    ]
    rec = record(sorters=sorters, reviewer=reviewer("contract", 0.97, subclass=reviewer_subclass),
                 extractions=[extract(0.95)])
    rec.diagnostics["extraction_candidates_collapsed"] = [
        {"doc_type": "contract", "subclass_differs": True,
         "doc_subclass": subclass, "extracted_doc_subclass": extracted}
    ]
    return rec


def test_a_reviewer_acceptance_relabels_the_subclass_before_the_extraction_guard():
    """The reviewer's accepted verdict sets the subclass exactly as review_classify_node does:
    a route arriving on a subclass the sweep never extracted may not reuse the first
    candidate's extraction, and an accepted subclass that WAS extracted still replays."""
    # the extraction ran under the sorter's lease hint; the reviewer re-labels to nda
    out = pr.replay(_reviewer_rec("lease", "lease", "nda"), POLICY)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["extraction_subclass_not_observed"]
    # control: the reviewer re-labels to the subclass that WAS extracted -> reuse is sound
    assert pr.replay(_reviewer_rec("lease", "lease", "lease"), POLICY).outcome == pr.AUTO


def test_a_reviewer_override_to_another_class_does_not_inherit_the_old_subclass():
    """Live reclassification replaces the subclass (None when the reviewer named none): a stale
    contract subclass must not make a correspondence route falsely unobserved."""
    rec = record(
        sorters=[
            SorterAttempt(attempt=1, doc_type="contract", doc_subclass="lease",
                          confidence_raw=0.85, confidence=0.85),
            SorterAttempt(attempt=2, doc_type="contract", doc_subclass="lease",
                          confidence_raw=0.86, confidence=0.86),
        ],
        reviewer=reviewer("correspondence", 0.97),
        extractions=[extract(0.95, doc_type="correspondence")],
    )
    rec.diagnostics["extraction_candidates_collapsed"] = [
        # the correspondence candidate was extracted with no subclass: the route's (cleared)
        # None matches, exactly as the live node's update would
        {"doc_type": "correspondence", "subclass_differs": True, "extracted_doc_subclass": None}
    ]
    out = pr.replay(rec, POLICY)
    assert out.outcome == pr.AUTO


# ---- T43: extraction evidence pools every scored attempt of the gold class ----------------------


def test_extraction_pairs_pool_every_scored_attempt_of_the_gold_class():
    rec = record(
        "p",
        sorters=[sorter(0.97)],
        extractions=[
            extract(0.97, attempt=1, doc_type="contract", score=0.2),  # confidently wrong; routing would accept it
            extract(0.70, attempt=2, doc_type="contract", score=0.95),
            extract(0.99, attempt=3, doc_type="contract", score=None),  # unscored: no evidence
            extract(0.99, attempt=1, doc_type="correspondence", score=0.1),  # another class
        ],
    )
    assert pr.extraction_pairs([rec]) == {"contract": [(0.97, False), (0.70, True)]}
    # a document whose sorter attempt 1 missed the gold class contributes nothing
    wrong_class = record("w", sorters=[sorter(0.97, doc_type="correspondence")], extractions=[extract(0.9)])
    assert pr.extraction_pairs([wrong_class]) == {}


def test_extraction_evidence_keeps_a_document_index_beside_the_pairs():
    two = record("a", sorters=[sorter(0.97)], extractions=[extract(0.9), extract(0.8, attempt=2)])
    one = record("b", sorters=[sorter(0.97)], extractions=[extract(0.9)])
    pairs, docs = pr.extraction_evidence([two, one])
    assert len(pairs["contract"]) == 3 and docs["contract"] == [0, 0, 1]


# ---- T41: an unknown schema passes the gate (parity with live routing) and is counted -----------


def test_unknown_schema_passes_like_live_routing_and_is_counted():
    known = record("k", sorters=[sorter(0.99)], extractions=[extract(0.99, schema_valid=True)])
    unknown = record("u", sorters=[sorter(0.99)], extractions=[extract(0.99, schema_valid=None)])
    invalid = record("i", sorters=[sorter(0.99)], extractions=[extract(0.99, schema_valid=False)])
    assert pr.replay(unknown, POLICY).outcome == pr.AUTO and pr.replay(unknown, POLICY).schema_unknown
    assert not pr.replay(known, POLICY).schema_unknown
    assert pr.replay(invalid, POLICY).outcome == pr.UNOBSERVED  # ... it needs the retry the record lacks
    res = pr.evaluate([known, unknown, unknown, invalid], POLICY)
    assert res["auto_archived_schema_unknown"] == 2 and res["outcomes"][pr.AUTO] == 3


def test_schema_unknown_counts_the_accepted_attempt_not_an_earlier_one():
    # attempt 1 unknown but below the cutoff; the retry that is archived has a verdict
    rec = record("r", sorters=[sorter(0.99)],
                 extractions=[extract(0.50, schema_valid=None), extract(0.97, attempt=2, schema_valid=True)])
    assert pr.replay(rec, POLICY).outcome == pr.AUTO and not pr.replay(rec, POLICY).schema_unknown
    rec = record("s", sorters=[sorter(0.99)],
                 extractions=[extract(0.50, schema_valid=True), extract(0.97, attempt=2, schema_valid=None)])
    assert pr.replay(rec, POLICY).schema_unknown
    assert pr.evaluate([rec], POLICY)["auto_archived_schema_unknown"] == 1


def test_schema_unknown_is_not_counted_for_human_review():
    rec = record("h", sorters=[sorter(0.99)], extractions=[extract(0.10, schema_valid=None)] * 1)
    res = pr.evaluate([rec], POLICY)
    assert res["outcomes"][pr.AUTO] == 0 and res["auto_archived_schema_unknown"] == 0


def _policy_file(tmp_path):
    import yaml

    path = tmp_path / "policy.yaml"
    cls = {"high": POLICY["high"], "low": POLICY["low"], "judge_band_high": POLICY["judge_band_high"]}
    # the taxonomy carries per-class overrides for contract: pin them to POLICY's values
    path.write_text(yaml.safe_dump({"confidence": {**POLICY, "by_class": {"contract": cls}}}))
    return path


def test_cli_prints_the_schema_unknown_and_handoff_diff_counts(tmp_path, capsys):
    path = tmp_path / "unk.jsonl"
    recs = [record(f"a{i}", sorters=[sorter(0.99)], extractions=[extract(0.99, schema_valid=True)]) for i in range(5)]
    recs += [record("u", sorters=[sorter(0.99)], extractions=[extract(0.99, schema_valid=None)])]
    pr.dump_records(recs, path)
    code, out, _ = _run_cli(["--records", path, "--policy", _policy_file(tmp_path), "--split", "test"], capsys)
    assert "schema evidence: 1 of 6 auto-archive(s) passed with schema_valid unknown" in out
    assert "replay fidelity (next to G3): 0 route(s)" in out
    assert out.strip().splitlines()[-1].startswith("SCORE:") and len(_score_lines(out)) == 1


# ---- T35: a confidence-only collapsed handoff stays observed but is counted ---------------------


def _collapsed(sub="nda", handoff="0.96", extracted="0.98", differs=False):
    return {"doc_type": "contract", "source": "sorter_2", "collapsed_onto": "sorter_1",
            "doc_subclass": sub, "handoff_confidence": handoff,
            "extracted_doc_subclass": sub, "extracted_handoff_confidence": extracted,
            "subclass_differs": differs}


def _sub_record(conf, collapsed, doc_id="c"):
    rec = record(doc_id, sorters=[SorterAttempt(attempt=1, doc_type="contract", doc_subclass="nda",
                                                confidence_raw=conf, confidence=conf)],
                 extractions=[extract(0.95)])
    rec.diagnostics["extraction_candidates_collapsed"] = collapsed
    return rec


def test_confidence_only_collapsed_handoff_is_replayed_and_counted():
    differing = _sub_record(0.96, [_collapsed()])  # the route arrives with the collapsed candidate's 0.96
    out = pr.replay(differing, POLICY)
    assert out.outcome == pr.AUTO and out.handoff_confidence_diff  # not demoted to unobserved
    # the route that arrives with the extracted candidate's own handoff (0.98) is an exact reuse
    exact = _sub_record(0.98, [_collapsed()])
    out = pr.replay(exact, POLICY)
    assert out.outcome == pr.AUTO and not out.handoff_confidence_diff
    plain = _sub_record(0.96, [])
    assert not pr.replay(plain, POLICY).handoff_confidence_diff
    res = pr.evaluate([differing, exact, plain], POLICY)
    assert res["replayed_with_confidence_only_handoff_diff"] == 1
    assert res["outcomes"][pr.UNOBSERVED] == 0 and res["unobserved_rate"] == 0.0  # G3 is not touched


def test_a_subclass_difference_still_makes_the_route_unobserved_and_is_not_counted_as_confidence_only():
    rec = _sub_record(0.96, [_collapsed(sub="lease", differs=True)])
    rec.diagnostics["extraction_candidates_collapsed"][0]["extracted_doc_subclass"] = "msa"
    res = pr.evaluate([rec], POLICY)
    assert res["outcomes"][pr.UNOBSERVED] == 1 and res["replayed_with_confidence_only_handoff_diff"] == 0


def test_handoff_diff_count_tolerates_older_diagnostics_without_the_confidence_keys():
    rec = _sub_record(0.96, [{"doc_type": "contract", "subclass_differs": False}])
    assert pr.replay(rec, POLICY).handoff_confidence_diff is False


def test_cli_prints_the_handoff_diff_count_next_to_g3(tmp_path, capsys):
    path = tmp_path / "h.jsonl"
    recs = [_sub_record(0.96, [_collapsed()], "d1"), _sub_record(0.96, [_collapsed()], "d2")]
    pr.dump_records(recs, path)
    _code, out, _ = _run_cli(["--records", path, "--policy", _policy_file(tmp_path), "--split", "test"], capsys)
    assert "replay fidelity (next to G3): 2 route(s) reused an extraction from a collapsed candidate" in out


# ---- #203 review: arbiter class, objective costs, document identity, ineligible attempts ---------


def _arbiter_cases(extractions_by_class):
    """Record whose sorter named ``contract`` and whose judge needs the arbiter on attempt 1."""
    return dict(
        sorters=[sorter(0.97)],
        extractions=extractions_by_class,
        judges=[judge("partial")],
    )


def test_arbiter_ruling_of_another_class_is_not_used():
    base = _arbiter_cases([extract(0.85, doc_type="contract")])
    other_first = [
        ArbiterObservation(ran=True, on_attempt=1, decision="human_review", doc_type="correspondence"),
        ArbiterObservation(ran=True, on_attempt=1, decision="accept_with_caveats", doc_type="contract"),
    ]
    out = pr.replay(record(**base, arbiters=other_first), POLICY)
    assert out.outcome == pr.AUTO  # the contract ruling, not the first one in the list
    only_other = [other_first[0]]
    out = pr.replay(record(**base, arbiters=only_other), POLICY)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["arbiter_not_observed_on_attempt_1"]


def test_untyped_arbiter_rulings_are_used_only_when_unambiguous():
    base = _arbiter_cases([extract(0.85)])
    one = [arbiter("accept_with_caveats")]
    assert pr.replay(record(**base, arbiters=one), POLICY).outcome == pr.AUTO  # unchanged for old records
    two = [arbiter("human_review"), arbiter("accept_with_caveats")]
    out = pr.replay(record(**base, arbiters=two), POLICY)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["arbiter_not_observed_on_attempt_1"]
    mixed = [ArbiterObservation(ran=True, on_attempt=1, decision="human_review", doc_type="correspondence"),
             arbiter("accept_with_caveats")]
    assert pr.replay(record(**base, arbiters=mixed), POLICY).outcome == pr.UNOBSERVED


@pytest.mark.parametrize("params", [
    {"c_human": 0.0}, {"c_human": -1.0}, {"c_error": float("nan")}, {"c_human": float("nan")},
    {"c_error": float("inf")}, {"c_error": 1.0}, {"c_error": 0.5},
])
def test_objective_costs_must_be_finite_and_ordered_before_scoring(params):
    rec = [record("x", sorters=[sorter(0.99)], extractions=[extract(0.99)])]
    with pytest.raises(ValueError, match="c_error > c_human > 0|finite"):
        pr.evaluate(rec, POLICY, params)
    with pytest.raises(ValueError):
        pr.accept_target(params.get("c_human", 1.0), params.get("c_error", 20.0))


def test_the_cli_rejects_a_non_finite_error_cost(tmp_path, capsys):
    ok = tmp_path / "ok.jsonl"
    pr.dump_records(_clean(3), ok)
    for bad in ("nan", "inf"):
        code, out, err = _run_cli(["--records", ok, "--c-error", bad], capsys)
        assert code == 1 and not _score_lines(out) and "finite" in err


def test_extraction_evidence_groups_repeated_sweeps_of_one_document():
    sweep_a = record("same", sorters=[sorter(0.97)], extractions=[extract(0.9), extract(0.8, attempt=2)])
    sweep_b = record("same", sorters=[sorter(0.97)], extractions=[extract(0.7)])
    other = record("other", sorters=[sorter(0.97)], extractions=[extract(0.9)])
    pairs, docs = pr.extraction_evidence([sweep_a, other, sweep_b])
    assert len(pairs["contract"]) == 4
    assert docs["contract"] == [0, 0, 1, 0]  # the same doc_id is one document, whatever the row position
    assert len(set(docs["contract"])) == 2


def test_extraction_evidence_never_merges_records_without_a_doc_id():
    a = record("", sorters=[sorter(0.97)], extractions=[extract(0.9)])
    b = record("", sorters=[sorter(0.97)], extractions=[extract(0.9)])
    _, docs = pr.extraction_evidence([a, b])
    assert docs["contract"] == [0, 1]


def test_attempts_routing_can_never_archive_are_not_archive_cutoff_evidence():
    rec = record(
        "p",
        sorters=[sorter(0.97)],
        extractions=[
            extract(0.97, attempt=1, schema_valid=False, score=0.1),  # retried whatever its confidence
            extract(0.97, attempt=2, hollow=True, score=0.0),  # hollow extractions retry too
            extract(0.95, attempt=3, schema_valid=True, score=0.95),
            extract(0.60, attempt=4, schema_valid=None, score=0.9),  # unknown schema passes like live routing
        ],
    )
    pairs, docs = pr.extraction_evidence([rec])
    assert pairs["contract"] == [(0.95, True), (0.60, True)] and docs["contract"] == [0, 0]
