#!/usr/bin/env python3
"""Run the LLM-as-a-judge evaluators over a pilot run.

Reads a pilot report (data/pilot_report.json), re-extracts raw text from each
sample PDF (direct parsing, no LLM), and runs the `judge` agent across the
task-spec dimensions:

  classification — is the sorter's assigned doc class correct for the document
                   (audited against the taxonomy task specification)?
  completeness   — did the specialist capture all fields the document states?
  correctness    — are the extracted field values factually accurate (no
                   fabrication)?

Scores are attached to each sample's deterministic Langfuse trace and a
calibration summary is printed and appended to the pilot report.

Usage:
    python scripts/run_quality_judges.py --real            # real judge LLM
    python scripts/run_quality_judges.py --mock            # deterministic fake
    python scripts/run_quality_judges.py --judges classification,completeness
    python scripts/run_quality_judges.py --report data/pilot_report.json --mock
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

# NOTE: no `OPENROUTER_API_KEY` placeholder is ever set here. Mock runs patch
# get_llm entirely (no key needed); real runs must resolve a REAL key from the
# environment — llm/providers.py rejects the historical "mock-key" placeholder,
# so a live run can never silently execute against fake credentials.

from scripts.prepare_samples import is_real_sample, prepare_samples  # noqa: E402

DEFAULT_REPORT = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "pilot_report.json"

MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"

JUDGES = ["classification", "completeness", "correctness"]

# score name -> (data_type, key-in-verdict, value-key-in-verdict)
_DIMENSION_SCORES = {
    "classification": [
        ("classification_correct", "classification_correct", "CATEGORICAL"),
        ("classification_quality", "classification_quality", "NUMERIC"),
    ],
    "completeness": [
        ("completeness", "completeness", "NUMERIC"),
        ("completeness_label", "completeness_label", "CATEGORICAL"),
    ],
    "correctness": [
        ("extraction_correctness", "extraction_correctness", "NUMERIC"),
        ("extraction_correctness_label", "extraction_correctness_label", "CATEGORICAL"),
    ],
}


def _fake_judge_client() -> MagicMock:
    def create(**kwargs):
        last = (kwargs.get("messages") or [{}])[-1]
        user_content = last.get("content", "") if isinstance(last, dict) else ""
        if "Audit the classification assignment" in user_content:
            content = json.dumps({
                "classification_correct": "correct",
                "classification_quality": 0.9,
                "reasoning": "mock judge",
            })
        elif "Evaluate extraction completeness" in user_content:
            content = json.dumps({
                "completeness": 0.95,
                "completeness_label": "complete",
                "reasoning": "mock judge",
            })
        elif "Audit the factual accuracy" in user_content:
            content = json.dumps({
                "extraction_correctness": 0.9,
                "extraction_correctness_label": "accurate",
                "reasoning": "mock judge",
            })
        else:
            content = json.dumps({"reasoning": "mock"})
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
    for dimension, scores in _DIMENSION_SCORES.items():
        if dimension not in verdict:
            continue
        reasoning = verdict[dimension].get("reasoning")
        for score_name, key, data_type in scores:
            value = verdict[dimension].get(key)
            if value is None:
                continue
            # Keep each dimension's evidence attached to its own score. Do not
            # combine notes into one shared score: independent judge runs must
            # remain independently queryable and must never overwrite or blur
            # another dimension's result.
            create_trace_score(
                trace_id,
                score_name,
                value,
                data_type=data_type,
                comment=reasoning,
            )
    logger.info("judge_scores_ingested", filename=sample["filename"], trace_id=trace_id)


def judge_one(sample: dict, mock_mode: bool, judges: list[str]) -> dict:
    extracted = sample.get("extracted_data") or {}

    doc_text = _raw_text_for(sample)
    if not doc_text.strip():
        return {"id": sample["id"], "status": "skipped", "reason": "no extractable source text"}

    from agents.judge import CompletenessJudge

    result = {
        "id": sample["id"],
        "doc_type": sample.get("doc_type"),
        "status": "judged",
    }
    started = time.perf_counter()
    doc_type = sample.get("doc_type", "")

    # Each dimension gets a fresh judge instance and its own failure boundary.
    # A missing extraction is still useful input: classification can run, while
    # completeness/correctness can honestly score the empty extraction.
    errors: dict[str, str] = {}

    def run_dimension(dimension: str) -> None:
        if dimension not in judges:
            return
        logger.info("judge_dimension_started", sample_id=sample["id"], dimension=dimension)
        try:
            judge = CompletenessJudge()
            if mock_mode:
                judge.client = _fake_judge_client()
                judge.model = "mock-model"
            if dimension == "classification":
                verdict = judge.judge_classification(doc_type, doc_text)
            elif dimension == "completeness":
                verdict = judge.judge_completeness(doc_type, extracted, doc_text)
            else:
                verdict = judge.judge_extraction_correctness(doc_type, extracted, doc_text)
            result[dimension] = verdict
            logger.info("judge_dimension_completed", sample_id=sample["id"], dimension=dimension)
        except Exception as exc:
            errors[dimension] = f"{type(exc).__name__}: {exc}"
            logger.exception("judge_dimension_failed", sample_id=sample["id"], dimension=dimension)

    run_dimension("classification")
    run_dimension("completeness")
    run_dimension("correctness")
    if errors:
        result["errors"] = errors
    result["judge_time_s"] = round(time.perf_counter() - started, 3)
    return result


def _dim_summary(results: list[dict], dimension: str, metric_key: str, label_key: str | None) -> dict:
    judged = [r for r in results if r["status"] == "judged" and dimension in r]
    if not judged:
        return {"n": 0}
    values = [r[dimension][metric_key] for r in judged if isinstance(r[dimension].get(metric_key), (int, float))]
    labels: dict[str, int] = {}
    if label_key:
        for r in judged:
            label = r[dimension].get(label_key)
            if label:
                labels[label] = labels.get(label, 0) + 1
    by_class: dict[str, dict] = {}
    for r in judged:
        cls = r.get("doc_type") or "unknown"
        val = r[dimension].get(metric_key)
        if not isinstance(val, (int, float)):
            continue
        b = by_class.setdefault(cls, {"n": 0, "sum": 0.0})
        b["n"] += 1
        b["sum"] += val
    return {
        "n": len(judged),
        "mean": round(sum(values) / len(values), 3) if values else None,
        "labels": labels,
        "per_class": {
            cls: {"n": v["n"], "mean": round(v["sum"] / v["n"], 3)}
            for cls, v in sorted(by_class.items())
        },
    }


def print_summary(stats: dict) -> None:
    print("\n== Evaluation summary ==")
    for dimension in JUDGES:
        s = stats.get(dimension) or {}
        if s.get("n", 0) == 0:
            print(f"{dimension:<16} not judged")
            continue
        print(f"{dimension:<16} n={s['n']:<3} mean={s.get('mean')} labels={s.get('labels')}")
        for cls, v in (s.get("per_class") or {}).items():
            print(f"  {cls:<24} n={v['n']:<3} mean={v['mean']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the quality judges over a pilot run.")
    parser.add_argument("--mock", action="store_true", help="Deterministic fake judge (no API key).")
    parser.add_argument("--real", action="store_true", help="Real judge via get_llm().")
    parser.add_argument("--judges", default=",".join(JUDGES), help=f"Comma-separated judges: {','.join(JUDGES)}")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Pilot report to read/update.")
    args = parser.parse_args()

    if args.mock and args.real:
        parser.error("choose --mock OR --real")
    if not args.mock and not args.real:
        parser.error(
            "choose --mock (deterministic fake judge) or --real (real LLM). "
            "The mode is explicit so a run can never silently execute against fake LLM results."
        )
    mock_mode = args.mock
    if mock_mode:
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
    else:
        # Real mode: fail fast before any judging if the OpenRouter key is
        # missing or is the mock placeholder. llm/providers.py enforces the
        # same check at every get_llm call.
        real_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not real_key or real_key == "mock-key":
            parser.error(
                "OPENROUTER_API_KEY is not set to a real key — refusing to run in --real mode."
            )

    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    invalid = [j for j in judges if j not in JUDGES]
    if invalid:
        parser.error(f"Unknown judge(s): {invalid}. Available: {', '.join(JUDGES)}")

    if not args.report.exists():
        parser.error(f"Pilot report not found: {args.report} (run scripts/run_pilot.py first)")

    prepare_samples()
    report = json.loads(args.report.read_text())
    samples = report.get("samples", [])

    # Real (non-mock) judging must only ever spend LLM tokens on real committed
    # legal documents. If the report contains repo-written synthetic samples
    # (e.g. produced by an earlier mock run), skip them — never judge fake
    # documents with the real judge LLM.
    if not mock_mode:
        real_ids = set()
        import csv as _csv

        with MANIFEST.open() as _fh:
            for _row in _csv.DictReader(_fh):
                if is_real_sample(_row):
                    real_ids.add(_row["id"])
        synthetic = [s for s in samples if s.get("id") not in real_ids]
        if synthetic:
            ids = ", ".join(s.get("id", "?") for s in synthetic)
            logger.warning(
                "real_judge_skipped_synthetic_samples",
                skipped_ids=ids,
                hint="synthetic .txt samples are mock-only; judge them with --mock",
            )
        samples = [s for s in samples if s.get("id") in real_ids]
        if not samples:
            parser.error(
                "No real samples in the report to judge. --real judging only "
                "processes actual committed legal documents; run the judge with "
                "--mock to include synthetic .txt samples."
            )
        logger.info("real_judge_sample_filter", remaining=len(samples))

    results = []
    for s in samples:
        verdict = judge_one(s, mock_mode, judges)
        results.append(verdict)
        if verdict["status"] == "judged" and not mock_mode:
            _ingest(s, verdict)

    if not mock_mode:
        from observability.tracing import flush

        flush()

    stats = {
        "classification": _dim_summary(results, "classification", "classification_quality", "classification_correct"),
        "completeness": _dim_summary(results, "completeness", "completeness", "completeness_label"),
        "correctness": _dim_summary(results, "correctness", "extraction_correctness", "extraction_correctness_label"),
    }
    print_summary(stats)

    evaluation_run = {
        "run_id": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if mock_mode else "real",
        "judges": judges,
        "summary": stats,
        "results": results,
    }
    evaluation = report.setdefault("evaluation", {})
    # Preserve every independent scoring iteration. `run` remains the latest
    # result for existing readers; `runs` is the append-only history.
    evaluation.setdefault("runs", []).append(evaluation_run)
    evaluation["run"] = evaluation_run
    args.report.write_text(json.dumps(report, indent=2))
    print(f"\nEvaluation report written to {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
