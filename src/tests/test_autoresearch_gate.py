"""The autoresearch keep/discard gate: core guards first, then the objective's eval."""

import importlib.util
import json
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


def _registry(tmp_path, eval_cmd, core=None, **objective):
    reg = {
        "core_guards": core if core is not None else {"compileall": OK},
        "objectives": {"obj": {"eval": eval_cmd, "metric": "m", "direction": "higher",
                               "min_delta": 0.01, **objective}},
    }
    path = tmp_path / "reg.yaml"
    path.write_text(yaml.safe_dump(reg))
    return path


def _gate(tmp_path, reg, desc="x"):
    return gate.main(["--objective", "obj", "--description", desc, "--registry", str(reg),
                      "--state-dir", str(tmp_path / "state")])


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


def test_parses_hillclimb_eval_output_format():
    out = "\n".join([
        "GUARD G1 PASS wrong-auto CP95 upper 0.0100 <= 0.02",
        "BASELINE GUARD G1 FAIL wrong-auto CP95 upper 0.0400 > 0.02",
        "DELTA U (candidate - baseline) = +0.0123  95% CI [+0.0040, +0.0210]  (significant; paired bootstrap, n=300)",
        "SCORE: +0.4100",
    ])
    parsed = gate.parse_eval(out)
    assert parsed == {"score": 0.41, "fails": [], "delta_ci": (0.004, 0.021)}
