#!/usr/bin/env python3
"""Export finished catalog documents + audit chains to Parquet warehouse files.

Writes under ``{MAILROOM_BASE_DIR}/warehouse/``::

    documents_YYYY-MM-DD.parquet
    audit_YYYY-MM-DD.parquet
    manifest.json

Usage:
    PYTHONPATH=src python src/scripts/export_warehouse.py
    PYTHONPATH=src python src/scripts/export_warehouse.py --full
    PYTHONPATH=src python src/scripts/export_warehouse.py --date 2026-08-27 --json
    PYTHONPATH=src python src/scripts/export_warehouse.py --doc-id abc-123
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

load_env()


async def _run(args: argparse.Namespace) -> dict:
    from storage.db import ensure_schema
    from storage.warehouse import export_to_warehouse

    ensure_schema()
    stamp = date.fromisoformat(args.date) if args.date else None
    doc_ids = [args.doc_id] if args.doc_id else None
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    return await export_to_warehouse(
        doc_ids=doc_ids,
        stamp=stamp,
        since=since,
        full=args.full,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="export all terminal documents (ignore watermark)")
    ap.add_argument("--date", help="stamp date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--since", help="only documents updated after this ISO timestamp")
    ap.add_argument("--doc-id", help="export a single document (must be archived or failed)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--show-manifest", action="store_true", help="print manifest.json and exit")
    args = ap.parse_args()

    if args.json:
        os.environ.setdefault("LOG_LEVEL", "ERROR")
    from pipeline.logging import setup_logging

    setup_logging()

    from storage.warehouse import load_warehouse_manifest, warehouse_dir

    if args.show_manifest:
        print(json.dumps(load_warehouse_manifest(), indent=2, default=str))
        print(f"warehouse_dir: {warehouse_dir()}", file=sys.stderr)
        return 0

    try:
        result = asyncio.run(_run(args))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        out = {k: v for k, v in result.items() if k != "manifest"}
        out["manifest_path"] = str(warehouse_dir() / "manifest.json")
        print(json.dumps(out, indent=2, default=str))
    else:
        status = result.get("status", "ok")
        print(f"status: {status}")
        if status == "skipped":
            print(f"reason: {result.get('reason')}")
        else:
            print(f"stamp: {result.get('stamp')}")
            print(f"batch documents: {result.get('exported_documents', 0)}")
            print(f"batch audit entries: {result.get('exported_audit_entries', 0)}")
            print(f"documents file total rows: {result.get('total_documents_in_file', 0)}")
            print(f"audit file total rows: {result.get('total_audit_in_file', 0)}")
            if result.get("documents_path"):
                print(f"documents: {result['documents_path']}")
            if result.get("audit_path"):
                print(f"audit: {result['audit_path']}")
        print(f"manifest: {warehouse_dir() / 'manifest.json'}")

    return 0 if result.get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
