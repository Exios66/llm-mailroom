#!/usr/bin/env python3
"""Judge extraction completeness against the source documents.

Reads a pilot report (data/pilot_report.json), re-extracts raw text from each
sample PDF (direct parsing, no LLM), runs the `judge` LLM agent to score
extraction completeness, and attaches the scores to each sample's deterministic
Langfuse trace. Also appends a completeness summary to the pilot report and
prints calibration stats.

Usage:
    python scripts/run_completeness_judge.py --real          # real judge LLM
    python scripts/run_completeness_judge.py --mock          # deterministic fake
    python scripts/run_completeness_judge.py --report data/pilot_report.json --mock
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import load_env  # noqa: E402

load_env()

os.environ.setdefault("OPENROUTER_API_KEY", "mock-key")

from scripts.prepare_samples import prepare_samples  # noqa: E402

DEFAULT_REPORT = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "pilot_report.json"


def _fake_judge_client() -> MagicMock:
    def create(**kwargs):
        last = (kwargs.get("messages") or [{}])[-1]
        user_content = last.get("content", "") if isinstance(last, dict) else ""
        if "Evaluate extraction completeness" in user_content:
            content = json.dumps({
                "completeness": 0.95,
                "completeness_label": "complete",
                "reasoning": "mock judge",
            })
        else:
            content = json.dumps({"completeness": 0.0, "completeness_label": "incomplete", "reasoning": "mock"})
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        return resp

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client


def _raw_text_for(sample: dict) -> str:
    from agents.pdf_transcriber import PDFTranscriber

    pdf = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "samples" / sample["subdir"] / sample["filename"]
    if not pdf.exists():
        logger.error("sample_pdf_missing", path=str(pdf))
        return ""
    try:
        text, _ = PDFTranscriber()._extract_raw_text(pdf)
        return text or ""
    except Exception:
        logger.exception("sample_text_extract_failed", path=str(pdf))
        return ""


def _ingest(sample: dict, verdict: dict) -> None:
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client
    from observability.scores import create_trace_score, ensure_score_configs, is_enabled

    if not is_enabled():
        return
    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        return
    try:
        trace_id = client.create_trace_id(seed=Path(sample["filename"]).stem)
    except Exception:
        logger.error("judge_trace_id_failed", filename=sample["filename"])
        return
    ensure_score_configs()
    create_trace_score(trace_id, "completeness", float(verdict["completeness"]), data_type="NUMERIC")
    create_trace_score(trace_id, "completeness_label", verdict["completeness_label"], data_type="CATEGORICAL")
    if verdict.get("reasoning"):
        create_trace_score(
            trace_id, "judge_notes", verdict["reasoning"][:500], data_type="TEXT"
        )
    logger.info("judge_scores_ingested", filename=sample["filename"], trace_id=trace_id)


def judge_one(sample: dict, mock_mode: bool) -> dict:
    extracted = sample.get("extracted_data") or {}
    if not extracted:
        return {"id": sample["id"], "status": "skipped", "reason": "no extracted_data"}

    doc_text = _raw_text_for(sample)
    if not doc_text.strip():
        return {"id": sample["id"], "status": "skipped", "reason": "no extractable source text"}

    from agents.judge import CompletenessJudge

    judge = CompletenessJudge()
    if mock_mode:
        judge.client = _fake_judge_client()
        judge.model = "mock-model"

    started = time.perf_counter()
    verdict = judge.judge_completeness(sample.get("doc_type", ""), extracted, doc_text)
    elapsed = round(time.perf_counter() - started, 3)

    return {
        "id": sample["id"],
        "doc_type": sample.get("doc_type"),
        "status": "judged",
        "completeness": verdict["completeness"],
        "completeness_label": verdict["completeness_label"],
        "reasoning": verdict["reasoning"],
        "judge_time_s": elapsed,
    }


def summarize(results: list[dict]) -> dict:
    judged = [r for r in results if r["status"] == "judged"]
    skipped = [r for r in results if r["status"] != "judged"]
    if not judged:
        return {"judged": 0, "skipped": len(skipped), "mean_completeness": None}
    scores = [r["completeness"] for r in judged]
    labels: dict[str, int] = {}
    for r in judged:
        labels[r["completeness_label"]] = labels.get(r["completeness_label"], 0) + 1
    by_class: dict[str, dict] = {}
    for r in judged:
        cls = r.get("doc_type") or "unknown"
        b = by_class.setdefault(cls, {"n": 0, "sum": 0.0})
        b["n"] += 1
        b["sum"] += r["completeness"]
    return {
        "judged": len(judged),
        "skipped": len(skipped),
        "mean_completeness": round(sum(scores) / len(scores), 3),
        "labels": labels,
        "per_class": {
            cls: {"n": v["n"], "mean_completeness": round(v["sum"] / v["n"], 3)}
            for cls, v in sorted(by_class.items())
        },
    }


def print_summary(stats: dict) -> None:
    print("\n== Completeness ===")
    if stats.get("mean_completeness") is None:
        print("Nothing judged — check the report path and extracted_data.")
        return
    print(f"judged: {stats['judged']} | skipped: {stats['skipped']} | "
          f"mean completeness: {stats['mean_completeness']}")
    print(f"labels: {stats['labels']}")
    for cls, v in stats["per_class"].items():
        print(f"  {cls:<24} n={v['n']:<3} mean_completeness={v['mean_completeness']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge extraction completeness of a pilot run.")
    parser.add_argument("--mock", action="store_true", help="Deterministic fake judge (no API key).")
    parser.add_argument("--real", action="store_true", help="Real judge via get_llm().")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Pilot report to read/update.")
    args = parser.parse_args()

    if args.mock and args.real:
        parser.error("choose --mock OR --real")
    mock_mode = not args.real
    if mock_mode:
        os.environ["OBSERVABILITY_PROVIDER"] = "none"

    if not args.report.exists():
        parser.error(f"Pilot report not found: {args.report} (run scripts/run_pilot.py first)")

    prepare_samples()
    report = json.loads(args.report.read_text())
    samples = report.get("samples", [])

    results = []
    for s in samples:
        verdict = judge_one(s, mock_mode)
        results.append(verdict)
        if verdict["status"] == "judged" and not mock_mode:
            _ingest(s, verdict)

    if not mock_mode:
        from observability.tracing import flush

        flush()

    stats = summarize(results)
    print_summary(stats)

    report.setdefault("completeness", {})["run"] = {
        "run_id": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if mock_mode else "real",
        "summary": stats,
        "results": results,
    }
    args.report.write_text(json.dumps(report, indent=2))
    print(f"\nCompleteness report written to {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
