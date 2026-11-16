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
