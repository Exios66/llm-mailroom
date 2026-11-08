"""Review-driven hardening of scripts/run_calibration_sweep.py (PR #203 threads T34-T38, T52-T54, T73)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import graph.build_graph as bg
from schemas.run_record import RunInfo
from scripts import run_calibration_sweep as sw

FIXTURE_ARGS = ["--dataset", "mailroom-fixtures", "--mock"]


@pytest.fixture
def ctx(temp_base_dir):
    run = RunInfo(run_id="t", dataset_id="unit", mode="mock", split="unsplit", gt_in_state=False)
    return sw.SweepContext(run=run, base_dir=temp_base_dir, mock=True, thresholds={"confidence": {}})


@pytest.fixture
def mocked():
    with sw.mock_llm(), sw.installed_hooks():
        yield


def _doc(doc_id="d1"):
    body = "This Master Services Agreement is made between Acme Corp and Beta LLC. " * 12
    return sw.Doc(doc_id=doc_id, filename=f"{doc_id}.txt", text=body, gold_class="contract")


def _run(argv, capsys):
    code = sw.main(argv)
    return code, capsys.readouterr()


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- T34 / T37: what full_information does and does not cover -------------------------------


def test_docs_and_diagnostics_say_arbiters_are_never_observed(ctx, mocked):
    assert "classification_arbiter" in sw.__doc__ and "ONLY" in sw.__doc__
    assert any("arbiter" in gap for gap in sw.KNOWN_GAPS)
    rec = sw.sweep_document(_doc(), ctx)
    assert rec.observed.full_information is True
    assert rec.diagnostics["unobserved_branches"] == ["classification_arbiter", "arbiter", "boss", "judge_on_retry_attempts"]
    assert rec.arbiters == []


# -- T52: failed chunks are recorded per attempt ---------------------------------------------


def test_failed_chunk_count_is_copied_from_the_agent(ctx):
    cap = sw._Capture()
    cap.agents = [SimpleNamespace(_last_n_chunks=4, _last_failed_chunks=1), SimpleNamespace()]
    step = sw.Step({}, {}, 0.0, 0, 0, 0.0, False, 0, cap)
    att = sw._extraction_attempt(_doc(), 1, "contract", step, "text", {})
    assert (att.n_chunks, att.failed_chunks) == (4, 1)
    assert not any("failed_chunks" in gap for gap in sw.KNOWN_GAPS)


def test_failed_chunks_default_to_zero_for_an_unchunked_extraction(ctx, mocked):
    rec = sw.sweep_document(_doc(), ctx)
    assert {e.failed_chunks for e in rec.extraction} == {0}


# -- T35: collapsed candidates are reported, a subclass difference is not full information -----


def _st(cls, sub, conf):
    return {"doc_type": cls, "doc_subclass": sub, "classification_confidence": conf}


def test_same_handoff_collapses_silently_different_handoff_is_reported():
    states = [("sorter_1", _st("contract", "nda", 0.931)), ("reviewer", _st("contract", "nda", 0.934)),
              ("sorter_2", _st("contract", "license", 0.90))]
    kept, dropped, collapsed = sw._pick_candidates(states, 2)
    assert [s for s, _ in kept] == ["sorter_1"] and dropped == []
    assert [c["source"] for c in collapsed] == ["sorter_2"]  # 0.931 and 0.934 both print as 0.93
    assert collapsed[0]["subclass_differs"] is True and collapsed[0]["collapsed_onto"] == "sorter_1"


def test_confidence_only_difference_is_listed_but_not_a_subclass_difference():
    states = [("sorter_1", _st("contract", "nda", 0.93)), ("sorter_2", _st("contract", "nda", 0.91))]
    _kept, _dropped, collapsed = sw._pick_candidates(states, 2)
    assert len(collapsed) == 1 and collapsed[0]["subclass_differs"] is False
    assert collapsed[0]["handoff_confidence"] == "0.91" and collapsed[0]["extracted_handoff_confidence"] == "0.93"


def test_a_reviewer_with_another_subclass_makes_the_record_partial(ctx, mocked, monkeypatch):
    real = sw._special_mock_content

    def other_subclass(text, expect):
        if "CLASSIFY THIS DOCUMENT" in text:
            return json.dumps({"doc_type": "contract", "contract_subtype": "agency", "doc_subclass": "agency",
                               "confidence": expect["conf"], "reasoning": "mock"})
        return real(text, expect)

    monkeypatch.setattr(sw, "_special_mock_content", other_subclass)
    rec = sw.sweep_document(_doc(), ctx)
    assert rec.error is None
    collapsed = rec.diagnostics.get("extraction_candidates_collapsed")
    assert collapsed, (rec.diagnostics["extraction_candidates"], rec.reviewer.reviewer_doc_subclass, [(a.doc_subclass, a.confidence) for a in rec.sorter])
    assert collapsed and collapsed[0]["source"] == "reviewer" and collapsed[0]["subclass_differs"]
    assert rec.observed.full_information is False
    assert {e.doc_type for e in rec.extraction} == {"contract"}  # observations are all kept


# -- T36: exhausted transient errors make the record an error, observations kept -------------


def test_exhausted_transient_extraction_marks_the_record_retryable(ctx, mocked, monkeypatch):
    monkeypatch.setattr(bg, "extract_node", lambda state: {"transient_error": True, "error_message": "429"})
    rec = sw.sweep_document(_doc(), ctx)
    assert rec.error.startswith("transient_error_exhausted") and "extract[contract#1]" in rec.error
    assert rec.diagnostics["transient_exhausted"] == ["extract[contract#1]"]
    assert [a.attempt for a in rec.sorter] == [1, 2] and rec.reviewer.ran  # partial observations survive
    assert rec.observed.full_information is False and rec.observed.sorter_attempts == 2


def test_transient_error_is_retried_by_resume_retry_errors(tmp_path, capsys, temp_base_dir, monkeypatch):
    out = tmp_path / "r.jsonl"
    real = bg.extract_node
    monkeypatch.setattr(bg, "extract_node", lambda state: {"transient_error": True, "error_message": "429"})
    code, cap = _run([*FIXTURE_ARGS, "--limit", "2", "--out", str(out)], capsys)
    assert code == 1 and json.loads(cap.out.split("SUMMARY ", 1)[1])["errors"] == 2
    monkeypatch.setattr(bg, "extract_node", real)
    code, _cap = _run([*FIXTURE_ARGS, "--limit", "2", "--resume", "--retry-errors", "--out", str(out)], capsys)
    rows = _lines(out)
    assert code == 0 and len(rows) == 2 and all(not r["error"] and r["observed"]["full_information"] for r in rows)


# -- T38: the rewrite is atomic ---------------------------------------------------------------


def test_rewrite_failure_leaves_the_original_and_no_temp_file(tmp_path, monkeypatch):
    out = tmp_path / "r.jsonl"
    out.write_text('{"doc_id": "a"}\n{"doc_id": "b"}\n')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(sw.os, "replace", boom)
    with pytest.raises(OSError):
        sw.atomic_write_lines(out, ['{"doc_id": "a"}'])
    assert out.read_text() == '{"doc_id": "a"}\n{"doc_id": "b"}\n'
    assert [p.name for p in tmp_path.iterdir()] == ["r.jsonl"]


def test_rewrite_replaces_the_file_with_the_given_lines(tmp_path):
    out = tmp_path / "r.jsonl"
    out.write_text("old\n")
    sw.atomic_write_lines(out, ['{"doc_id": "é"}'])
    assert out.read_text(encoding="utf-8") == '{"doc_id": "é"}\n'
    assert [p.name for p in tmp_path.iterdir()] == ["r.jsonl"]


# -- T53: a torn tail is repaired even without --retry-errors ---------------------------------


def test_resume_repairs_a_torn_tail_without_retry_errors(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "r.jsonl"
    assert _run([*FIXTURE_ARGS, "--limit", "1", "--out", str(out)], capsys)[0] == 0
    out.write_text(out.read_text() + '{"doc_id": "torn", "run"')  # no trailing newline
    code, cap = _run([*FIXTURE_ARGS, "--limit", "2", "--resume", "--out", str(out)], capsys)
    assert code == 0 and "unparsable" in cap.err
    text = out.read_text()
    assert text.endswith("\n")
    rows = [json.loads(line) for line in text.splitlines()]  # every line parses
    assert len(rows) == 2 and "torn" not in {r["doc_id"] for r in rows}


def test_resume_adds_the_missing_newline_of_a_complete_last_record(tmp_path, capsys, temp_base_dir):
    out = tmp_path / "r.jsonl"
    assert _run([*FIXTURE_ARGS, "--limit", "1", "--out", str(out)], capsys)[0] == 0
    out.write_text(out.read_text().rstrip("\n"))
    assert _run([*FIXTURE_ARGS, "--limit", "2", "--resume", "--out", str(out)], capsys)[0] == 0
    assert len(_lines(out)) == 2


# -- T54: resume only continues records from the same inputs ----------------------------------


def _first_run(tmp_path, capsys):
    out = tmp_path / "r.jsonl"
    assert _run([*FIXTURE_ARGS, "--limit", "2", "--out", str(out)], capsys)[0] == 0
    return out


def _refused(argv, out, capsys):
    before = out.read_text()
    code, cap = _run(argv, capsys)
    assert code == 2 and "refusing --resume" in cap.err
    assert out.read_text() == before  # a refused resume never touches the file
    return cap.err


def test_resume_refuses_other_judge_settings(tmp_path, capsys, temp_base_dir):
    out = _first_run(tmp_path, capsys)
    err = _refused([*FIXTURE_ARGS, "--limit", "3", "--resume", "--judge-attempts", "all", "--out", str(out)], out, capsys)
    assert "judge_attempts" in err


def test_resume_refuses_a_changed_taxonomy(tmp_path, capsys, temp_base_dir, monkeypatch):
    out = _first_run(tmp_path, capsys)
    monkeypatch.setattr(sw, "_taxonomy_sha256", lambda: "0" * 64)
    assert "taxonomy_sha256" in _refused([*FIXTURE_ARGS, "--limit", "3", "--resume", "--out", str(out)], out, capsys)


def test_resume_refuses_changed_thresholds(tmp_path, capsys, temp_base_dir, monkeypatch):
    out = _first_run(tmp_path, capsys)
    monkeypatch.setenv("MAILROOM_JUDGE_VERIFY", "off")
    assert "thresholds_in_force" in _refused([*FIXTURE_ARGS, "--limit", "3", "--resume", "--out", str(out)], out, capsys)


def test_resume_refuses_a_changed_document(tmp_path, capsys, temp_base_dir):
    out = _first_run(tmp_path, capsys)
    rows = _lines(out)
    rows[0]["content_sha256"] = "f" * 64
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "content_sha256" in _refused([*FIXTURE_ARGS, "--limit", "3", "--resume", "--out", str(out)], out, capsys)


def test_resume_refuses_another_dataset_or_mode(tmp_path, capsys, temp_base_dir):
    out = _first_run(tmp_path, capsys)
    rows = _lines(out)
    rows[0]["run"]["dataset_id"] = "manifest:other.csv"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "dataset_id" in _refused([*FIXTURE_ARGS, "--limit", "3", "--resume", "--out", str(out)], out, capsys)


def test_resume_refuses_records_that_do_not_record_their_provenance(tmp_path, capsys, temp_base_dir):
    out = _first_run(tmp_path, capsys)
    rows = _lines(out)
    del rows[0]["diagnostics"]["judge_attempts"]
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert "not recorded" in _refused([*FIXTURE_ARGS, "--limit", "3", "--resume", "--out", str(out)], out, capsys)


def test_resume_with_matching_inputs_still_works(tmp_path, capsys, temp_base_dir):
    out = _first_run(tmp_path, capsys)
    code, cap = _run([*FIXTURE_ARGS, "--limit", "3", "--resume", "--out", str(out)], capsys)
    assert code == 0 and "(2 already done)" in cap.out and len(_lines(out)) == 3


# -- T73: the sweep never touches a configured live database or a .env-only base dir -----------


def test_database_url_is_unset_during_the_sweep_and_restored(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://live/db")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from storage import db

    with sw.sweep_environment(True, None) as base:
        assert "DATABASE_URL" not in os.environ
        assert db._resolve_url() == f"sqlite+aiosqlite:///{base / 'mailroom.db'}"
    assert os.environ["DATABASE_URL"] == "postgresql+asyncpg://live/db"


def test_database_url_unset_stays_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    with sw.sweep_environment(True, None):
        pass
    assert "DATABASE_URL" not in os.environ


def test_base_dir_that_only_dotenv_supplies_is_not_used(tmp_path, capsys, monkeypatch):
    live = tmp_path / "live-data"
    monkeypatch.delenv("MAILROOM_BASE_DIR", raising=False)
    monkeypatch.setattr("pipeline.env.load_env", lambda *a, **k: os.environ.setdefault("MAILROOM_BASE_DIR", str(live)))
    code, cap = _run([*FIXTURE_ARGS, "--limit", "1", "--out", str(tmp_path / "r.jsonl")], capsys)
    assert code == 0 and not live.exists()
    assert "base_dir=" in cap.out and str(live) not in cap.out
