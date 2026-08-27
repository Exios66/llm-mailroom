#!/usr/bin/env python3
"""Parse and analyze the full local audit DB (and optional catalog join).

Summarizes every hash-chained audit entry in ``data/mailroom.db``: event/actor
histograms, per-document chain health, review-related events, and recent
activity. Companion to ``verify_audit_chains.py`` (which only checks hashes).

Usage:
    PYTHONPATH=src python src/scripts/analyze_audit_db.py
    PYTHONPATH=src python src/scripts/analyze_audit_db.py --json
    PYTHONPATH=src python src/scripts/analyze_audit_db.py --no-verify --recent 50
    PYTHONPATH=src python src/scripts/analyze_audit_db.py --join-catalog
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

load_env()
from pipeline.logging import setup_logging  # noqa: E402

setup_logging()


async def _run(args: argparse.Namespace) -> dict:
    from storage.audit_log import analyze_audit_db
    from storage.db import ensure_schema

    ensure_schema()
    report = await analyze_audit_db(verify_chains=not args.no_verify, event_limit=args.recent)
    if args.join_catalog:
        from collections import Counter

        from storage.catalog import list_documents

        docs = await list_documents()
        by_stage = Counter(d.stage for d in docs)
        by_type = Counter(d.doc_type or "(unclassified)" for d in docs)
        report["catalog"] = {
            "documents": len(docs),
            "by_stage": dict(by_stage),
            "by_doc_type": dict(by_type),
        }
    return report


def _print_human(report: dict) -> None:
    print(f"Audit entries:     {report['total_entries']}")
    print(f"Documents:         {report['distinct_documents']}")
    print(f"Matters:           {report['distinct_matters']}")
    if report.get("chains_checked") is not None:
        print(
            f"Chains checked:    {report['chains_checked']} "
            f"(broken: {report.get('chains_broken', 0)})"
        )
    print("\nBy event:")
    for event, count in (report.get("by_event") or {}).items():
        print(f"  {count:6d}  {event}")
    print("\nBy actor:")
    for actor, count in (report.get("by_actor") or {}).items():
        print(f"  {count:6d}  {actor}")
    if report.get("review_events"):
        print("\nReview-related:")
        for event, count in report["review_events"].items():
            print(f"  {count:6d}  {event}")
    if report.get("catalog"):
        cat = report["catalog"]
        print(f"\nCatalog documents: {cat['documents']}")
        print("  by stage:", cat["by_stage"])
        print("  by type: ", cat["by_doc_type"])
    recent = report.get("recent_events") or []
    if recent:
        print(f"\nRecent events (n={len(recent)}):")
        for e in recent[:20]:
            print(
                f"  {e.get('timestamp') or '?'}  {e.get('event')}  "
                f"doc={e.get('doc_id')}  actor={e.get('actor')}"
            )
    broken = [c for c in (report.get("chains") or []) if not c.get("ok")]
    if broken:
        print(f"\nBroken chains ({len(broken)}):")
        for c in broken[:20]:
            print(f"  {c['doc_id']}  entries={c['entries']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable JSON")
    ap.add_argument("--no-verify", action="store_true", help="skip hash-chain verification")
    ap.add_argument("--recent", type=int, default=20, help="recent events to include")
    ap.add_argument(
        "--join-catalog",
        action="store_true",
        help="also summarize documents/matters stage and type counts",
    )
    args = ap.parse_args()
    report = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)
    broken = report.get("chains_broken") or 0
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
