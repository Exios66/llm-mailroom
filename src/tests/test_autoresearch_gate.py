"""The autoresearch keep/discard gate: core guards first, then the objective's eval."""

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("autoresearch_gate", ROOT / "src/scripts/autoresearch_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

PY = sys.executable
OK = f'{PY} -c "pass"'


def _echo(*lines, code=0):
    body = ";".join(f"print({line!r})" for line in lines)
    return f'{PY} -c "{body};raise SystemExit({code})"'


def _echo_script(tmp_path, *lines, code=0):
    """Like _echo but a real script file: lines that contain quotes (e.g. the
    hillclimb `records: ... modes=['mock']` line) cannot be quoted through -c + shlex."""
    script = tmp_path / "echo_eval.py"
    script.write_text("import sys\n" + "".join(f"print({line!r})\n" for line in lines)
                      + f"raise SystemExit({code})\n")
    return f'{shlex.quote(PY)} {shlex.quote(str(script))}'


def _registry(tmp_path, eval_cmd, core=None, **objective):
    reg = {
        "core_guards": core if core is not None else {"compileall": OK},
        "objectives": {"obj": {"eval": eval_cmd, "metric": "m", "direction": "higher",
                               "min_delta": 0.01, **objective}},
    }
    path = tmp_path / "reg.yaml"
    path.write_text(yaml.safe_dump(reg))
    return path


def _gate(tmp_path, reg, desc="x", **flags):
    argv = ["--objective", "obj", "--description", desc, "--registry", str(reg),
            "--state-dir", str(tmp_path / "state")]
    for key, value in flags.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return gate.main(argv)


def _results(tmp_path):
    rows = [json.loads(line) for line in (tmp_path / "state/obj.jsonl").read_text().splitlines()]
    assert rows[0]["type"] == "config"
    return [r for r in rows if r["type"] == "result"]


def test_baseline_keep_discard_sequence(tmp_path):
    assert _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.5000"))) == 0
    assert _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.5200"))) == 0
    assert _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.5250"))) == 3  # gain < min_delta
    assert _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.4000"))) == 3
    assert [r["status"] for r in _results(tmp_path)] == ["baseline", "keep", "discard", "discard"]
    assert (tmp_path / "state/obj_dashboard.md").read_text().count("| discard |") == 2


def test_failing_core_guard_discards_without_running_eval(tmp_path):
    marker = tmp_path / "eval_ran"
    eval_cmd = f'{PY} -c "open({str(marker)!r},\'w\').write(\'1\');print(\'SCORE: 9.0\')"'
    reg = _registry(tmp_path, eval_cmd, core={"compileall": OK, "experiment_gate": f'{PY} -c "raise SystemExit(1)"'})
    assert _gate(tmp_path, reg) == 2
    (row,) = _results(tmp_path)
    assert row["status"] == "guard_fail" and row["reasons"] == ["core:experiment_gate"]
    assert not marker.exists()


def test_affected_tests_none_selected_is_a_pass(tmp_path):
    reg = _registry(tmp_path, _echo("SCORE: 0.1"), core={"affected_tests": f'{PY} -c "raise SystemExit(5)"'})
    assert _gate(tmp_path, reg) == 0


def test_eval_guard_failure_line_wins_over_a_higher_score(tmp_path):
    _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.1")))
    reg = _registry(tmp_path, _echo("GUARD G1 FAIL 9/10 wrong", "SCORE: 0.9"))
    assert _gate(tmp_path, reg) == 2
    assert _results(tmp_path)[-1]["reasons"] == ["eval:G1"]


def test_gate_line_from_stress_test_fails(tmp_path):
    assert _gate(tmp_path, _registry(tmp_path, _echo("GATE lost_docs FAIL  (value=3)", "SCORE: 0.0000"), )) == 2


def test_crash_on_missing_score_or_bad_exit(tmp_path):
    assert _gate(tmp_path, _registry(tmp_path, _echo("no score here"))) == 1
    assert _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.5", code=1))) == 1


def test_delta_ci_must_exclude_zero_when_required(tmp_path):
    line = "DELTA U (candidate - baseline) = +0.0100  95% CI [{}, {}] (paired bootstrap)"
    reg = _registry(tmp_path, _echo(line.format("-0.0020", "0.0200"), "SCORE: 0.5"), require_delta_ci=True)
    assert _gate(tmp_path, reg) == 2
    reg = _registry(tmp_path, _echo(line.format("+0.0010", "0.0200"), "SCORE: 0.5"), require_delta_ci=True)
    assert _gate(tmp_path, reg) == 0


def test_guards_only_objective(tmp_path):
    assert _gate(tmp_path, _registry(tmp_path, _echo("3 passed"), direction="guards_only")) == 0
    assert _gate(tmp_path, _registry(tmp_path, _echo("1 failed", code=2), direction="guards_only")) == 2


def test_real_registry_declares_metric_guards_and_evidence_for_every_objective():
    reg = yaml.safe_load((ROOT / "experiments/autoresearch/objectives.yaml").read_text())
    assert {"compileall", "affected_tests", "experiment_gate", "contract", "changelog"} <= set(reg["core_guards"])
    assert {"thresholds", "scale", "review"} <= set(reg["objectives"])
    for name, obj in reg["objectives"].items():
        for key in ("goal", "eval", "metric", "direction", "min_delta", "guards", "evidence"):
            assert obj.get(key) not in (None, "", []), f"{name} lacks {key}"
        assert obj["direction"] in ("higher", "lower", "guards_only")
        script = obj["eval"].split()[1] if obj["eval"].split()[1].startswith("src/") else None
        if script:
            assert (ROOT / script).exists(), f"{name} eval script {script} missing"
    assert reg["objectives"]["thresholds"]["require_delta_ci"] is True


def test_unknown_objective(tmp_path):
    reg = _registry(tmp_path, _echo("SCORE: 1"))
    assert gate.main(["--objective", "nope", "--description", "x", "--registry", str(reg),
                      "--state-dir", str(tmp_path / "s")]) == 1


@pytest.mark.parametrize("line,expected", [("SCORE: -0.2139", -0.2139), ("SCORE: 1", 1.0)])
def test_parse_score(line, expected):
    assert gate.parse_eval(line)["score"] == expected


@pytest.mark.parametrize("line,expected", [
    ("records: 300 (split=test, modes=['real']); accept target 1 - C_h/C_e = 0.700", ["real"]),
    ("records: 3 (split=test, modes=['mock']); accept target 1 - C_h/C_e = 0.700", ["mock"]),
    ("records: 2 (split=test, modes=['mock', 'real'])", ["mock", "real"]),
])
def test_parse_modes(line, expected):
    assert gate.parse_eval(line)["modes"] == expected


# --- T15: paths with spaces must reach the evaluator as a single argument ---

def test_path_with_spaces_stays_one_argument(tmp_path):
    dump = tmp_path / "argv_dump.json"
    lines = [_records_line("'real'"), "SCORE: 0.5000"]
    probe = tmp_path / "probe_eval.py"
    probe.write_text("import json, sys\n"
                     f"open({str(dump)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                     + "".join(f"print({line!r})\n" for line in lines))
    records = tmp_path / "with space" / "rec.jsonl"
    candidate = tmp_path / "with space" / "candidate.yaml"
    baseline = tmp_path / "with space" / "baseline.yaml"
    records.parent.mkdir()
    records.write_text("")
    eval_cmd = (f'{shlex.quote(PY)} {shlex.quote(str(probe))} --records {{records}} '
                '--policy {candidate} --baseline-policy {baseline}')
    assert _gate(tmp_path, _registry(tmp_path, eval_cmd), records=str(records),
                 candidate=str(candidate), baseline=str(baseline)) == 0
    assert json.loads(dump.read_text()) == [
        "--records", str(records), "--policy", str(candidate), "--baseline-policy", str(baseline)]


# --- T7: mock/synthetic records are never evidence ---

def _records_line(modes: str) -> str:
    return f"records: 12 (split=test, modes=[{modes}]); accept target 1 - C_h/C_e = 0.700"


def test_mock_mode_records_are_recorded_but_not_evidence(tmp_path):
    records = str(tmp_path / "mock.jsonl")
    reg = _registry(tmp_path, _echo_script(tmp_path, _records_line("'mock'"), "SCORE: 0.9000"))
    assert _gate(tmp_path, reg, records=records, baseline="current.yaml") == 4
    (row,) = _results(tmp_path)
    assert row["status"] == "harness" and row["reasons"] == ["records modes mock are not evidence (not real)"]
    assert row["modes"] == ["mock"] and row["records"] == records and row["baseline"] == "current.yaml"
    # The harness row must not seed best_kept...
    assert gate.best_kept([row], "higher") is None
    # ...and a real-records run still baselines as the first scored result.
    real_reg = _registry(tmp_path, _echo_script(tmp_path, _records_line("'real'"), "SCORE: 0.9000"))
    assert _gate(tmp_path, real_reg, records=str(tmp_path / "real.jsonl")) == 0
    real_row, = [r for r in _results(tmp_path) if r["status"] == "baseline"]
    assert real_row["best_before"] is None


def test_mixed_modes_still_not_evidence_and_only_real_records_baseline(tmp_path):
    reg = _registry(tmp_path, _echo_script(tmp_path, _records_line("'mock', 'real'"), "SCORE: 1.0"))
    assert _gate(tmp_path, reg) == 4
    real_reg = _registry(tmp_path, _echo_script(tmp_path, _records_line("'real'"), "SCORE: 0.5000"))
    assert _gate(tmp_path, real_reg) == 0


# --- T14: the first candidate must clear min_delta, and deltas respect direction ---

_DELTA_LINE = ("DELTA U (candidate - baseline) = {point}  95% CI [{lo}, {hi}]  "
               "(significant; paired bootstrap, n=300)")


def test_first_candidate_delta_below_min_delta_discards(tmp_path):
    line = _DELTA_LINE.format(point="+0.0010", lo="+0.0002", hi="+0.0200")
    reg = _registry(tmp_path, _echo(line, "SCORE: 0.5010"), require_delta_ci=True, min_delta=0.005)
    assert _gate(tmp_path, reg) == 3
    (row,) = _results(tmp_path)
    assert row["status"] == "discard" and row["best_before"] is None
    assert row["reasons"] == ["first candidate delta +0.0010 < min_delta 0.0050"]
    assert row["delta_vs_policy"] == pytest.approx(0.0010)


def test_first_candidate_delta_clearing_min_delta_baselines(tmp_path):
    line = _DELTA_LINE.format(point="+0.0100", lo="+0.0040", hi="+0.0200")
    reg = _registry(tmp_path, _echo(line, "SCORE: 0.5100"), require_delta_ci=True, min_delta=0.005)
    assert _gate(tmp_path, reg) == 0
    (row,) = _results(tmp_path)
    assert row["status"] == "baseline" and row["delta_vs_policy"] == pytest.approx(0.0100)
    # A later iteration now needs the usual gain over the seeded bar.
    reg2 = _registry(tmp_path, _echo(line, "SCORE: 0.5105"), require_delta_ci=True, min_delta=0.005)
    assert _gate(tmp_path, reg2) == 3


def test_first_candidate_without_require_delta_ci_baselines_regardless_of_delta(tmp_path):
    reg = _registry(tmp_path, _echo(_DELTA_LINE.format(point="+0.0010", lo="+0.0002", hi="+0.0200"),
                                    "SCORE: 0.5010"))
    assert _gate(tmp_path, reg) == 0


def test_lower_direction_delta_is_direction_aware(tmp_path):
    reg = _registry(tmp_path, _echo("SCORE: 0.5000"), direction="lower")  # min_delta 0.01
    assert _gate(tmp_path, reg) == 0
    # A worse candidate for a lower-is-better metric must record a NEGATIVE delta.
    reg2 = _registry(tmp_path, _echo("SCORE: 0.6000"), direction="lower")
    assert _gate(tmp_path, reg2) == 3
    discard = _results(tmp_path)[-1]
    assert discard["status"] == "discard" and discard["delta"] == pytest.approx(-0.1)
    # A better candidate records a positive delta and keeps.
    reg3 = _registry(tmp_path, _echo("SCORE: 0.4800"), direction="lower")
    assert _gate(tmp_path, reg3) == 0
    keep = _results(tmp_path)[-1]
    assert keep["status"] == "keep" and keep["delta"] == pytest.approx(0.02)


def test_real_registry_review_metric_states_what_the_gate_does():
    reg = yaml.safe_load((ROOT / "experiments/autoresearch/objectives.yaml").read_text())
    review = reg["objectives"]["review"]
    # The gate reads no metric for guards_only objectives: the wording says so.
    assert "guards only" in review["metric"]
    assert "owner-run evidence" in review["metric"]
    assert "not a gate decision" in review["metric"]


def test_parses_hillclimb_eval_output_format():
    out = "\n".join([
        "GUARD G1 PASS wrong-auto CP95 upper 0.0100 <= 0.02",
        "BASELINE GUARD G1 FAIL wrong-auto CP95 upper 0.0400 > 0.02",
        "DELTA U (candidate - baseline) = +0.0123  95% CI [+0.0040, +0.0210]  (significant; paired bootstrap, n=300)",
        "records: 300 (split=test, modes=['real']); accept target 1 - C_h/C_e = 0.700",
        "SCORE: +0.4100",
    ])
    parsed = gate.parse_eval(out)
    assert parsed == {"score": 0.41, "fails": [], "delta_ci": (0.004, 0.021),
                      "delta": 0.0123, "modes": ["real"]}


def test_parse_eval_without_modes_or_delta_lines_is_unchanged_behaviour():
    parsed = gate.parse_eval("SCORE: 0.5\nrecords: 3 (split=test)")
    assert parsed == {"score": 0.5, "fails": [], "delta_ci": None, "delta": None, "modes": None}


# --- #203 review: provenance, point estimate, formatting, guards-only modes ---

def test_records_objective_without_modes_line_is_not_evidence(tmp_path):
    """A thresholds-style eval (reads {records}) that omits the modes line or reports an empty list
    must not baseline or keep: its provenance is unknown."""
    for lines in (["SCORE: 0.9000"], [_records_line("")] + ["SCORE: 0.9000"]):
        reg = _registry(tmp_path, _echo_script(tmp_path, *lines) + " {records}")
        assert _gate(tmp_path, reg, records=str(tmp_path / "r.jsonl")) == 4
    rows = _results(tmp_path)
    assert [r["status"] for r in rows] == ["harness", "harness"]
    assert "provenance is unknown" in rows[0]["reasons"][0]
    assert gate.best_kept(rows, "higher") is None


def test_objective_without_records_does_not_need_a_modes_line(tmp_path):
    """scale-style objectives read no records, so a missing modes line stays fine."""
    assert _gate(tmp_path, _registry(tmp_path, _echo("SCORE: 0.5000"))) == 0


def test_guards_only_pass_with_mock_records_is_harness(tmp_path):
    reg = _registry(tmp_path, _echo_script(tmp_path, _records_line("'mock'"), "3 passed"),
                    direction="guards_only")
    assert _gate(tmp_path, reg) == 4
    assert _results(tmp_path)[-1]["status"] == "harness"
    ok = _registry(tmp_path, _echo_script(tmp_path, _records_line("'real'"), "3 passed"),
                   direction="guards_only")
    assert _gate(tmp_path, ok) == 0


def test_first_candidate_without_delta_point_estimate_is_not_baselined(tmp_path):
    line = "DELTA U (candidate - baseline) 95% CI [+0.0040, +0.0200]  (significant)"
    reg = _registry(tmp_path, _echo(line, "SCORE: 0.5100"), require_delta_ci=True, min_delta=0.005)
    assert _gate(tmp_path, reg) == 1
    (row,) = _results(tmp_path)
    assert row["status"] == "crash" and row["reasons"] == ["no DELTA U point estimate to check against min_delta"]
    assert gate.best_kept([row], "higher") is None


def test_unformattable_eval_command_is_a_recorded_crash(tmp_path):
    reg = _registry(tmp_path, 'echo {oops}')
    assert _gate(tmp_path, reg) == 1
    (row,) = _results(tmp_path)
    assert row["status"] == "crash" and row["reasons"] == ["eval exit 1"]


def test_core_failure_is_recorded_even_when_the_eval_command_cannot_be_formatted(tmp_path):
    reg = _registry(tmp_path, 'echo {oops}', core={"compileall": f'{PY} -c "raise SystemExit(1)"'})
    assert _gate(tmp_path, reg) == 2
    (row,) = _results(tmp_path)
    assert row["status"] == "guard_fail" and row["reasons"] == ["core:compileall"]
