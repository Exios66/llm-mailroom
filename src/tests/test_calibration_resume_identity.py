"""#247: ``run_calibration_sweep.py --resume`` identity covers the selection settings, and
``calibrate_thresholds.py --folds`` rejects a value cross-fitting cannot use.

Everything runs on the mock LLM and the committed fixtures; the HF corpus loader is replaced by
the fixture loader, so nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemas.run_record import RunInfo, RunRecord
from scripts import calibrate_thresholds as ct
from scripts import run_calibration_sweep as sw


def _run(argv, capsys):
    code = sw.main(argv)
    return code, capsys.readouterr()


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def hf_like(monkeypatch):
    """``--hf-split`` served by the fixtures, so a seed / per-class run needs no network."""
    def fake(split, per_class, seed):
        docs, ds = sw.load_fixture_docs()
        return docs, {**ds, "dataset_id": "hf-like", "dataset_revision": "rev0", "split": split}

    monkeypatch.setattr(sw, "load_hf_docs", fake)


def _hf(*extra):
    return ["--hf-split", "train", "--mock", *extra]


# ---- the run block records the selection settings ------------------------------------------


def test_run_info_selection_fields_are_optional_so_legacy_rows_still_load():
    assert RunInfo(run_id="r", mode="mock").seed is None
    legacy = json.dumps({"run_id": "r", "mode": "mock", "split": "train"})
    info = RunInfo.model_validate_json(legacy)
    assert (info.seed, info.per_class, info.limit) == (None, None, None)


def test_rows_record_seed_per_class_and_limit_for_a_sampled_corpus(tmp_path, capsys, temp_base_dir, hf_like):
    out = tmp_path / "rec.jsonl"
    code, _ = _run(_hf("--seed", "7", "--per-class", "3", "--limit", "2", "--out", str(out)), capsys)
    assert code == 0
    for row in _lines(out):
        assert (row["run"]["seed"], row["run"]["per_class"], row["run"]["limit"]) == (7, 3, 2)
        RunRecord.model_validate(row)


def test_seed_and_per_class_are_inert_for_fixtures_and_recorded_as_none(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    assert _run(["--dataset", "mailroom-fixtures", "--mock", "--seed", "9", "--limit", "2", "--out", str(out)],
                capsys)[0] == 0
    assert {(r["run"]["seed"], r["run"]["per_class"], r["run"]["limit"]) for r in _lines(out)} == {(None, None, 2)}
    # a different (inert) seed on a fixtures resume is not a mismatch
    code, cap = _run(["--dataset", "mailroom-fixtures", "--mock", "--seed", "1", "--limit", "3", "--resume",
                      "--out", str(out)], capsys)
    assert code == 0 and "refusing" not in cap.err


# ---- resume refuses a changed selection ----------------------------------------------------


def test_resume_with_a_changed_seed_is_refused(tmp_path, capsys, temp_base_dir, hf_like):
    out = tmp_path / "rec.jsonl"
    assert _run(_hf("--seed", "1", "--limit", "2", "--out", str(out)), capsys)[0] == 0
    before = out.read_text()
    code, cap = _run(_hf("--seed", "2", "--limit", "2", "--resume", "--out", str(out)), capsys)
    assert code == 2
    assert "refusing --resume" in cap.err and "seed: saved 1, now 2" in cap.err
    assert out.read_text() == before


def test_resume_with_a_changed_per_class_is_refused(tmp_path, capsys, temp_base_dir, hf_like):
    out = tmp_path / "rec.jsonl"
    assert _run(_hf("--per-class", "2", "--limit", "2", "--out", str(out)), capsys)[0] == 0
    code, cap = _run(_hf("--per-class", "3", "--limit", "2", "--resume", "--out", str(out)), capsys)
    assert code == 2 and "per_class: saved 2, now 3" in cap.err


def test_resume_with_the_same_selection_continues(tmp_path, capsys, temp_base_dir, hf_like):
    out = tmp_path / "rec.jsonl"
    assert _run(_hf("--seed", "4", "--limit", "2", "--out", str(out)), capsys)[0] == 0
    code, cap = _run(_hf("--seed", "4", "--limit", "3", "--resume", "--out", str(out)), capsys)
    assert code == 0 and "1 document(s) (2 already done)" in cap.out


def test_resume_may_raise_the_limit_but_not_lower_it(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    base = ["--dataset", "mailroom-fixtures", "--mock", "--out", str(out)]
    assert _run([*base, "--limit", "3"], capsys)[0] == 0
    code, cap = _run([*base, "--limit", "2", "--resume"], capsys)
    assert code == 2 and "limit: saved 3, now 2" in cap.err
    assert _run([*base, "--limit", "4", "--resume"], capsys)[0] == 0
    assert _run([*base, "--resume"], capsys)[0] == 0  # no limit: the rest of the fixtures


def _row(**run):
    return {"run": {"mode": "mock", "dataset_id": "d", "dataset_revision": None, "split": "unsplit",
                    "taxonomy_sha256": "t", **run},
            "diagnostics": {"judge_attempts": "first", "max_extraction_candidates": 2},
            "thresholds_in_force": {}}


_CURRENT = {"mode": "mock", "dataset_id": "d", "dataset_revision": None, "split": "unsplit",
            "taxonomy_sha256": "t", "judge_attempts": "first", "max_extraction_candidates": 2,
            "thresholds": {}, "seed": 1, "per_class": 5, "limit": None}


def test_legacy_rows_without_the_selection_fields_are_refused():
    why = sw.resume_mismatch([_row()], [], _CURRENT)
    assert why is not None
    for key in ("seed", "per_class", "limit"):
        assert f"{key}: not recorded in the saved records" in why


def test_allow_legacy_waives_only_the_missing_selection_fields():
    assert sw.resume_mismatch([_row()], [], _CURRENT, allow_legacy=True) is None
    # a recorded value that differs is still a mismatch
    why = sw.resume_mismatch([_row(seed=2)], [], _CURRENT, allow_legacy=True)
    assert why is not None and "seed: saved 2, now 1" in why
    # every other identity check still applies
    why = sw.resume_mismatch([_row(taxonomy_sha256="other")], [], _CURRENT, allow_legacy=True)
    assert why is not None and "taxonomy_sha256" in why
    legacy_missing_diag = {**_row(), "diagnostics": {}}
    why = sw.resume_mismatch([legacy_missing_diag], [], _CURRENT, allow_legacy=True)
    assert why is not None and "judge_attempts: not recorded" in why
    assert sw.legacy_rows([_row(), _row(seed=1, per_class=5, limit=None)]) == 1


def _strip_selection(path: Path) -> None:
    """Rewrite a sweep file as a pre-#247 checkout would have written it."""
    rows = _lines(path)
    for row in rows:
        for key in sw.LEGACY_RUN_KEYS:
            row["run"].pop(key, None)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_legacy_file_resume_is_refused_by_default_with_a_hint(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    base = ["--dataset", "mailroom-fixtures", "--mock", "--out", str(out)]
    assert _run([*base, "--limit", "2"], capsys)[0] == 0
    _strip_selection(out)
    before = out.read_text()
    code, cap = _run([*base, "--limit", "3", "--resume"], capsys)
    assert code == 2 and "--allow-legacy-resume" in cap.err
    assert out.read_text() == before


def test_legacy_file_resumes_with_the_flag_and_warns(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "rec.jsonl"
    base = ["--dataset", "mailroom-fixtures", "--mock", "--out", str(out)]
    assert _run([*base, "--limit", "2"], capsys)[0] == 0
    _strip_selection(out)
    code, cap = _run([*base, "--limit", "3", "--resume", "--allow-legacy-resume"], capsys)
    assert code == 0
    assert "warning: --allow-legacy-resume: 2 saved record(s)" in cap.err
    assert "1 document(s) (2 already done)" in cap.out
    rows = _lines(out)
    assert len(rows) == 3 and rows[-1]["run"]["limit"] == 3  # new rows record the selection


def test_allow_legacy_resume_needs_resume(tmp_path, capsys):
    code, cap = _run(["--dataset", "mailroom-fixtures", "--mock", "--out", str(tmp_path / "r.jsonl"),
                      "--allow-legacy-resume"], capsys)
    assert code == 2 and "needs --resume" in cap.err


def test_resume_mismatch_accepts_a_matching_selection():
    row = _row(seed=1, per_class=5, limit=None)
    assert sw.resume_mismatch([row], [], _CURRENT) is None


# ---- calibrate_thresholds --folds ----------------------------------------------------------


@pytest.mark.parametrize("folds", ["1", "0", "-3"])
def test_folds_below_two_is_refused_without_a_traceback(folds, tmp_path, capsys):
    recs = tmp_path / "r.jsonl"
    recs.write_text("")
    with pytest.raises(SystemExit) as exc:
        ct.main(["--records", str(recs), "--out", str(tmp_path / "o.yaml"), "--folds", folds])
    err = capsys.readouterr().err
    assert exc.value.code != 0
    assert "--folds must be >= 2" in err and "Traceback" not in err
    assert not (tmp_path / "o.yaml").exists()


def test_folds_two_is_accepted_by_the_parser(tmp_path, capsys):
    # an empty record file is a bad-input exit (1), reached only after --folds parsed
    recs = tmp_path / "r.jsonl"
    recs.write_text("")
    assert ct.main(["--records", str(recs), "--out", str(tmp_path / "o.yaml"), "--folds", "2"]) == 1
    assert "no records" in capsys.readouterr().err


def test_negative_limit_is_a_usage_error_and_a_saved_negative_limit_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        sw.main(["--dataset", "mailroom-fixtures", "--mock", "--out", str(tmp_path / "r.jsonl"), "--limit", "-1"])
    assert exc.value.code == 2
    why = sw.resume_mismatch([_row(seed=1, per_class=5, limit=-1)], [], {**_CURRENT, "limit": 1})
    assert why is not None and "limit: saved -1 is not a valid limit" in why


def test_a_saved_bool_limit_is_not_a_valid_limit():
    """``isinstance(True, int)`` is true; a recorded ``true`` must not pass as the limit 1."""
    why = sw.resume_mismatch([_row(seed=1, per_class=5, limit=True)], [], {**_CURRENT, "limit": 1})
    assert why is not None and "limit: saved True is not a valid limit" in why
    # a real integer still passes, and False is refused like True
    assert sw.resume_mismatch([_row(seed=1, per_class=5, limit=1)], [], {**_CURRENT, "limit": 1}) is None
    why = sw.resume_mismatch([_row(seed=1, per_class=5, limit=False)], [], {**_CURRENT, "limit": 1})
    assert why is not None and "not a valid limit" in why


@pytest.mark.parametrize("key,saved,now", [("seed", True, 1), ("per_class", True, 1), ("seed", False, 0)])
def test_a_saved_bool_never_equals_an_integer_selection_setting(key, saved, now):
    """``True == 1`` in Python; a bool in the saved row is a different identity from the integer."""
    current = {**_CURRENT, key: now}
    run = {"seed": 1, "per_class": 5, "limit": None, key: saved}
    why = sw.resume_mismatch([_row(**run)], [], current)
    assert why is not None and f"{key}: saved {saved!r}, now {now!r}" in why
    # the same integer still matches
    run[key] = now
    assert sw.resume_mismatch([_row(**run)], [], current) is None
