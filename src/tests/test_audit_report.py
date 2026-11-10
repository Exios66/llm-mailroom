"""Roadmap M12: ``audit-report`` (spec §7, #235 acceptance 3) and the scoring-block Langfuse scores."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from tests.test_warehouse_audit import wh  # noqa: F401  (fixture)

GOLDEN = Path(__file__).parent / "fixtures" / "audit" / "audit_report.md"


def _scoring(overall, f1, f2, headline=None, status="scored"):
    return {"overall_score": overall, "headline_score": headline, "extraction_f1": f1, "extraction_f2": f2,
            "status": status, "scored_payload": "model"}


def _seed(doc, doc_type, *, event="archived", model=None, scoring=None, adherence=None, metrics=None):
    from audit.row import build_row
    from audit.writer import write_terminal_row
    from storage.catalog import write_document_record

    state = {"doc_id": doc, "matter_id": "M", "doc_type": doc_type, "scoring": scoring,
             "extraction_adherence": {"schema_adherence": adherence} if adherence is not None else None,
             "audit_trace": [{"node": "extract", "model": model}] if model else []}
    row = build_row(state, event=event, actor="archivist", metrics=metrics)

    async def go():
        await write_document_record({"doc_id": doc, "matter_id": "M", "original_filename": f"{doc}.txt",
                                     "stage": "failed" if event == "failed" else "archived",
                                     "doc_type": doc_type})
        await write_terminal_row(row)

    asyncio.run(go())


@pytest.fixture
def corpus(wh):  # noqa: F811
    _seed("c-1", "contract", model="deepseek/deepseek-v4-flash", scoring=_scoring(0.5, 0.6, 0.7), adherence=0.9,
          metrics={"success_rate": 1, "class_correct": 1, "run_duration_seconds": 2.0})
    _seed("c-2", "contract", model="deepseek/deepseek-v4-flash", scoring=_scoring(0.7, 0.8, 0.9), adherence=1.0,
          metrics={"success_rate": 0, "class_correct": 1, "run_duration_seconds": 4.0})
    _seed("m-1", "merger_agreement", model="mailroom-extract", scoring=_scoring(0.4, 0.5, None, headline=0.6),
          adherence=0.75, metrics={"success_rate": 1})
    _seed("k-1", "correspondence")
    _seed("u-1", "unknown", event="failed")
    return wh


def test_audit_report_golden(corpus):
    from audit.report import render_report

    out = asyncio.run(render_report())
    assert out == GOLDEN.read_text()
    # Spot checks on the golden's meaning, so a regenerated golden cannot hide a regression.
    contract = out.split("## contract", 1)[1].split("\n## ", 1)[0]
    assert "| Schema | ContractExtraction |" in contract and "| n | 2 |" in contract
    assert "| Route | API (2) |" in contract and "| extraction_f1 | 0.7000 (n=2) |" in contract
    merger = out.split("## merger_agreement", 1)[1].split("\n## ", 1)[0]
    assert "| Route | Modal (1) |" in merger and "| extraction_f2 | not collected |" in merger
    assert "| overall_extraction_score | 0.6000 (n=1) |" in merger  # the headline score
    letters = out.split("## correspondence", 1)[1].split("\n## ", 1)[0]
    assert "| Model | not collected |" in letters and "| extraction_f1 | not collected |" in letters
    assert "unknown: 1" in out


def test_report_reads_db_or_parquet_identically(corpus):
    from audit.report import render_report

    ids = ["c-1", "c-2", "m-1", "k-1", "u-1"]
    asyncio.run(corpus.export_to_warehouse(doc_ids=ids[:2], stamp=date(2026, 11, 3)))
    asyncio.run(corpus.export_to_warehouse(doc_ids=ids, stamp=date(2026, 11, 4)))  # c-1, c-2 exported twice
    from_db = asyncio.run(render_report())
    from_parquet = asyncio.run(render_report(parquet_dir=corpus.warehouse_dir()))
    assert from_parquet == from_db


def test_cli_writes_report(corpus, tmp_path, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "audit_report_cli", Path(__file__).resolve().parents[1] / "scripts" / "audit_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "report.md"
    assert mod.main(["--out", str(out)]) == 0
    assert out.read_text() == GOLDEN.read_text()


def test_langfuse_scoring_emission_names(monkeypatch):
    from llm_dojo_scoring import load_registry

    from observability import scores

    sent = []
    monkeypatch.setattr(scores, "is_enabled", lambda: True)
    monkeypatch.setattr(scores, "score_trace", lambda name, value, **kw: sent.append((name, value, kw)))
    out = scores.emit_scoring_scores(_scoring(0.5, 0.6, None), {"schema_adherence": 0.9}, trace_id="t-1")
    assert out == {"extraction_overall_score": 0.5, "extraction_f1": 0.6, "schema_adherence": 0.9}
    assert [n for n, _, _ in sent] == ["extraction_overall_score", "extraction_f1", "schema_adherence"]
    registered = {c["name"] for c in scores.SCORE_CONFIGS}
    assert all(n in registered and n in load_registry().metrics for n, _, _ in sent)
    assert all(kw["data_type"] == "NUMERIC" and kw["score_id"] == f"t-1-{n}" for n, _, kw in sent)
    # No Langfuse: nothing is attached, the numbers still come back.
    sent.clear()
    monkeypatch.setattr(scores, "is_enabled", lambda: False)
    assert scores.emit_scoring_scores(_scoring(0.5, 0.6, 0.7), None) == {
        "extraction_overall_score": 0.5, "extraction_f1": 0.6, "extraction_f2": 0.7}
    assert sent == []
    assert scores.emit_scoring_scores(None, None) == {}


def test_run_pipeline_emits_the_row_scoring_block(temp_base_dir, mocker, monkeypatch):
    from graph import build_graph as bg
    from observability import scores
    from tests.test_audit_trace import FIELDS, TEXT

    monkeypatch.delenv("DATABASE_URL", raising=False)
    mocker.patch("agents.sorter.SorterAgent.classify_json", return_value={
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "Agreement"})
    mocker.patch.object(bg, "_extract_contracts", side_effect=lambda *a: {**FIELDS, "confidence": 0.99})
    spy = mocker.spy(scores, "emit_scoring_scores")
    source = temp_base_dir / "pipeline" / "inbox" / "agreement.txt"
    source.write_text(TEXT)
    result = bg.run_pipeline(source, "M-SCORE")
    assert result["stage"] == "archived"
    assert spy.call_count == 1
    args, kwargs = spy.call_args
    assert args[0] == result.get("scoring") and args[1] == result.get("extraction_adherence")
    assert kwargs["trace_id"] == result.get("trace_id")
