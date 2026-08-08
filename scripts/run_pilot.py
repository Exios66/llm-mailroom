#!/usr/bin/env python3
"""Pilot-test the pipeline against examples/samples/manifest.csv.

Feeds each sample through the real LangGraph pipeline, records per-document
outcomes (stage, doc_type, confidence, retries, LLM call count, wall time), and
scores them against the ground truth in the manifest. Two modes:

  --mock   deterministic fake LLM (returns the expected classification). No API
           key needed. Tests the pipeline *machinery* (PDF ingestion/transcribe,
           routing, retries, archiving, timing) reproducibly — not LLM accuracy.
  --real   real LLM via get_llm(). Requires OPENROUTER_API_KEY in .env.
           Measures actual classification/extraction accuracy too.

Use --baseline <report.json> to diff two runs (e.g. before/after a procedural
change) and quantify accuracy/time/call deltas.

Usage:
    python scripts/run_pilot.py --mock
    python scripts/run_pilot.py --real
    python scripts/run_pilot.py --mock --baseline data/pilot_report_baseline.json
    python scripts/run_pilot.py --mock --include contract
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import load_env  # noqa: E402

load_env()

os.environ.setdefault("OPENROUTER_API_KEY", "mock-key")
# Tracing mode is set in main() from the CLI flag: --mock forces it off so
# mock runs stay hermetic; --real leaves .env's auto resolution (-> langfuse).

from scripts.prepare_samples import prepare_samples  # noqa: E402

MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"

_LLM_METRICS = {"calls": 0, "seconds": 0.0}


def _fake_client(expect: dict) -> MagicMock:
    def create(**kwargs):
        start = time.perf_counter()
        _LLM_METRICS["calls"] += 1
        content = "Mock free-form output (report / transcription)."
        rf = kwargs.get("response_format") or {}
        name = (rf.get("json_schema") or {}).get("name", "")
        if name == "sorter_output":
            content = json.dumps({
                "doc_type": expect["doc_type"],
                "confidence": expect["conf"],
                "reasoning": "mock",
            })
        elif name == "boss_output":
            content = json.dumps({"decision": "approved", "reasoning": "mock", "resolution_notes": ""})
        elif name.endswith("_output"):
            content = json.dumps({"confidence": expect["conf"], "mock_extraction": True})
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        _LLM_METRICS["seconds"] += time.perf_counter() - start
        return resp

    client = MagicMock()
    client.chat.completions.create.side_effect = create
    return client


def run_sample(sample: dict, mock_mode: bool) -> dict:
    from pipeline.bins import inbox_dir
    from graph.build_graph import run_pipeline

    matter_id = f"PILOT-{sample['id']}"
    sample_pdf = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "samples" / sample["subdir"] / sample["filename"]

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    queued = inbox / sample["filename"]
    shutil.copyfile(sample_pdf, queued)

    expect = {
        "doc_type": sample["expected_doc_class"],
        "conf": 0.40 if sample["id"] == "ambiguous_01" else 0.95,
    }

    _LLM_METRICS["calls"] = 0
    _LLM_METRICS["seconds"] = 0.0

    def _mock_get_llm(agent_name):
        return _fake_client(expect), "mock-model"

    started = time.perf_counter()
    if mock_mode:
        with patch("llm.client.get_llm", side_effect=_mock_get_llm), \
             patch("agents.base.get_llm", side_effect=_mock_get_llm):
            result = run_pipeline(queued, matter_id)
    else:
        result = run_pipeline(queued, matter_id)
    wall = time.perf_counter() - started

    return {
        "id": sample["id"],
        "filename": sample["filename"],
        "expected_doc_class": sample["expected_doc_class"],
        "expected_stage": sample["expected_stage"],
        "size_tier": sample["size_tier"],
        "actual_doc_class": result.get("doc_type"),
        "classification_confidence": result.get("classification_confidence"),
        "extraction_confidence": result.get("extraction_confidence"),
        "stage": result.get("stage"),
        "classification_attempts": result.get("classification_attempts", 0),
        "extraction_attempts": result.get("extraction_attempts", 0),
        "retry_count": result.get("retry_count", 0),
        "wall_time_s": round(wall, 3),
        "llm_calls": _LLM_METRICS["calls"],
        "llm_time_s": round(_LLM_METRICS["seconds"], 3),
        "class_match": result.get("doc_type") == sample["expected_doc_class"],
        "stage_expected": result.get("stage") == sample["expected_stage"],
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    archived = sum(1 for r in rows if r["stage"] == "archived")
    review = sum(1 for r in rows if r["stage"] == "review")
    failed = sum(1 for r in rows if r["stage"] == "failed")
    class_matches = sum(1 for r in rows if r["class_match"])
    per_class: dict[str, dict] = {}
    for r in rows:
        pc = per_class.setdefault(r["expected_doc_class"], {"n": 0, "match": 0, "time": 0.0, "calls": 0})
        pc["n"] += 1
        pc["match"] += int(r["class_match"])
        pc["time"] += r["wall_time_s"]
        pc["calls"] += r["llm_calls"]
    return {
        "samples": n,
        "archived": archived,
        "review": review,
        "failed": failed,
        "class_accuracy": round(class_matches / n, 3) if n else 0,
        "avg_time_s": round(sum(r["wall_time_s"] for r in rows) / n, 3) if n else 0,
        "avg_llm_calls": round(sum(r["llm_calls"] for r in rows) / n, 1) if n else 0,
        "per_class": {
            cls: {
                "n": v["n"],
                "class_accuracy": round(v["match"] / v["n"], 3),
                "avg_time_s": round(v["time"] / v["n"], 3),
                "avg_llm_calls": round(v["calls"] / v["n"], 1),
            }
            for cls, v in sorted(per_class.items())
        },
    }


def diff_report(new: dict, baseline: dict) -> dict:
    keys = ["samples", "archived", "review", "failed", "class_accuracy", "avg_time_s", "avg_llm_calls"]
    overall = {k: {"baseline": baseline.get(k), "now": new.get(k)} for k in keys}
    per_class = {}
    for cls, b in (baseline.get("per_class") or {}).items():
        c = (new.get("per_class") or {}).get(cls, {})
        per_class[cls] = {
            k: {"baseline": b.get(k), "now": c.get(k)}
            for k in ("n", "class_accuracy", "avg_time_s", "avg_llm_calls")
        }
    return {"overall": overall, "per_class": per_class}


def print_rows(rows: list[dict]) -> None:
    header = f"{'id':<24}{'class':<18}{'exp_stage':<10}{'stage':<10}{'exp_class':<18}{'act_class':<18}{'conf':<5}{'calls':<6}{'time_s':<8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['id']:<24}{r['expected_doc_class']:<18}{r['expected_stage']:<10}{str(r['stage']):<10}"
            f"{r['expected_doc_class']:<18}{str(r['actual_doc_class']):<18}"
            f"{str(r['classification_confidence']):<5}{r['llm_calls']:<6}{r['wall_time_s']:<8}"
        )


def print_summary(summary: dict) -> None:
    print("\n== Summary ==")
    print(f"samples: {summary['samples']} | archived: {summary['archived']} | "
          f"review: {summary['review']} | failed: {summary['failed']}")
    print(f"class_accuracy: {summary['class_accuracy']} | avg_time_s: {summary['avg_time_s']} | "
          f"avg_llm_calls: {summary['avg_llm_calls']}")
    for cls, s in summary["per_class"].items():
        print(f"  {cls:<20} n={s['n']:<2} acc={s['class_accuracy']:<6} "
              f"avg_time_s={s['avg_time_s']:<8} avg_calls={s['avg_llm_calls']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the pilot sample set through the pipeline.")
    parser.add_argument("--mock", action="store_true", help="Use a deterministic fake LLM (no API key).")
    parser.add_argument("--real", action="store_true", help="Use the real LLM (needs OPENROUTER_API_KEY).")
    parser.add_argument("--include", help="Only run samples of this expected doc class (e.g. contract).")
    parser.add_argument("--baseline", help="Path to a previous pilot report JSON to diff against.")
    args = parser.parse_args()

    if args.mock and args.real:
        parser.error("choose --mock OR --real")
    mock_mode = not args.real
    if mock_mode:
        # Mock runs must never send traces (fake LLM, no real data).
        os.environ["OBSERVABILITY_PROVIDER"] = "none"

    prepare_samples()

    with MANIFEST.open() as fh:
        manifest = list(csv.DictReader(fh))
    if args.include:
        manifest = [m for m in manifest if m["expected_doc_class"] == args.include]

    rows = [run_sample(m, mock_mode) for m in manifest]

    summary = summarize(rows)
    print_rows(rows)
    print_summary(summary)

    report = {
        "run_id": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if mock_mode else "real",
        "summary": summary,
        "samples": rows,
    }
    out_path = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "pilot_report.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {out_path}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text())
        print("\n== Diff vs baseline ==")
        print(json.dumps(diff_report(summary, baseline["summary"]), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
