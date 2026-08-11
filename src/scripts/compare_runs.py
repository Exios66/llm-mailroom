#!/usr/bin/env python3
"""Compare document runs against each other using catalog metrics.

Reads the local SQLite catalog (or DATABASE_URL Postgres) and tabulates the
per-run core metrics that every run now records — duration, token usage,
estimated cost, LLM call count, attempts, confidences, and abort status — so
runs can be evaluated against one another without touching Langfuse.

Usage:
    python scripts/compare_runs.py                 # table + per-doc-type aggregate
    python scripts/compare_runs.py --limit 50      # most recent 50 runs
    python scripts/compare_runs.py --doc-type contract
    python scripts/compare_runs.py --json          # machine-readable output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

load_env()

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

METRIC_COLUMNS = (
    "run_aborted",
    "run_duration_seconds",
    "total_tokens",
    "estimated_cost_usd",
    "llm_call_count",
    "classification_attempts",
    "extraction_attempts",
    "classification_confidence",
    "extraction_confidence",
    "stage_completed",
    "parse_error",
    "schema_valid",
    "guardrail_triggered",
)


def _scores_of(record) -> dict:
    scores = record.scores or {}
    return {k: scores.get(k) for k in METRIC_COLUMNS}


def _cell(value, width: int) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:.3f}".rjust(width)
    return str(value).rjust(width)


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("No runs found in the catalog.")
        return
    headers = ["doc_id", "matter", "type", "stage", *METRIC_COLUMNS]
    widths = {h: max(len(h), *(len(str(r.get(h, "-"))) for r in rows)) for h in headers}
    header_line = "  ".join(h.rjust(widths[h]) for h in headers)
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        cells = [row.get(h, "-") for h in headers]
        print("  ".join(_cell(c, widths[h]) for c, h in zip(cells, headers)))


def _aggregate(rows: list[dict]) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row.get("type") or "unknown"].append(row)

    print("\n== Aggregate by document type ==")
    print(f"{'doc_type':<20} {'runs':>5} {'archived%':>9} {'aborted':>7} {'avg_cost':>9} "
          f"{'avg_tokens':>10} {'avg_dur_s':>9} {'avg_cls_conf':>11} {'avg_ext_conf':>11}")
    for doc_type, group in sorted(groups.items()):
        n = len(group)
        archived = sum(1 for r in group if r.get("stage") == "archived")
        aborted = sum(1 for r in group if r.get("run_aborted"))
        avg_cost = sum(r.get("estimated_cost_usd") or 0 for r in group) / n
        avg_tokens = sum(r.get("total_tokens") or 0 for r in group) / n
        avg_dur = sum(r.get("run_duration_seconds") or 0 for r in group) / n
        confs = [r.get("classification_confidence") for r in group if r.get("classification_confidence") is not None]
        avg_cls = sum(confs) / len(confs) if confs else 0.0
        ext_confs = [r.get("extraction_confidence") for r in group if r.get("extraction_confidence") is not None]
        avg_ext = sum(ext_confs) / len(ext_confs) if ext_confs else 0.0
        print(f"{doc_type:<20} {n:>5} {100 * archived / n:>8.1f}% {aborted:>7} "
              f"{avg_cost:>9.4f} {avg_tokens:>10.0f} {avg_dur:>9.1f} {avg_cls:>11.3f} {avg_ext:>11.3f}")

    total_cost = sum(r.get("estimated_cost_usd") or 0 for r in rows)
    total_tokens = sum(r.get("total_tokens") or 0 for r in rows)
    archived = sum(1 for r in rows if r.get("stage") == "archived")
    aborted = sum(1 for r in rows if r.get("run_aborted"))
    print("\n== Totals ==")
    print(f"runs: {len(rows)}  archived: {archived} ({100 * archived / max(len(rows), 1):.1f}%)  "
          f"aborted: {aborted}  total_tokens: {total_tokens}  est_cost_usd: {total_cost:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Only the most recent N runs.")
    parser.add_argument("--doc-type", default=None, help="Filter to one document type.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables.")
    args = parser.parse_args()

    from storage.catalog import list_documents

    records = asyncio.run(list_documents(limit=args.limit))
    rows = []
    for rec in records:
        if args.doc_type and rec.doc_type != args.doc_type:
            continue
        scores = _scores_of(rec)
        row = {
            "doc_id": rec.doc_id[:8],
            "matter": rec.matter_id,
            "type": rec.doc_type or "unknown",
            "stage": rec.stage,
        }
        row.update(scores)
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    _print_table(rows)
    _aggregate(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
