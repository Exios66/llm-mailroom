#!/usr/bin/env python3
"""Verify every hash-chained audit chain in the database (audit A-8).

Recomputes each per-doc chain with the shipped algorithm (v2, with v1
fallback for legacy rows) and reports breaks. Nonzero exit when any chain is
broken — so restore/backup flows can gate on it.

Usage:
    python scripts/verify_audit_chains.py                 # full report
    python scripts/verify_audit_chains.py --doc d1        # single doc
    python scripts/verify_audit_chains.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

load_env()
from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

from storage.db import ensure_schema  # noqa: E402


async def _all_chains():
    from sqlalchemy import select, distinct
    from storage.audit_log import AuditLogRecord, get_audit_chain

    ensure_schema()
    from storage.db import async_session

    async with async_session() as session:
        rows = await session.execute(select(distinct(AuditLogRecord.doc_id)))
        doc_ids = [r[0] for r in rows.all()]
    chains = {}
    for doc_id in doc_ids:
        chains[doc_id] = await get_audit_chain(doc_id)
    return chains


def _verify(doc_id: str, records: list[dict]) -> dict:
    from schemas.audit import AuditLogEntry, verify_chain

    if not records:
        return {"doc_id": doc_id, "ok": True, "entries": 0, "error": None}
    entries = [
        AuditLogEntry(
            entry_id=r["entry_id"], doc_id=doc_id, matter_id=r.get("matter_id", ""),
            event=r["event"], actor=r["actor"], detail=r["detail"],
            prev_hash=r["prev_hash"], entry_hash=r["entry_hash"], timestamp=r["timestamp"],
        )
        for r in records
    ]
    ok = verify_chain(entries)
    # Locate the first broken link for the report.
    error = None
    if not ok:
        for i, e in enumerate(entries):
            expected_prev = "" if i == 0 else entries[i - 1].entry_hash
            if e.prev_hash != expected_prev:
                error = f"link {i}: prev_hash mismatch (expected {expected_prev[:12]}, got {e.prev_hash[:12]})"
                break
        if error is None:
            error = "entry hash mismatch (content tampered)"
    return {"doc_id": doc_id, "ok": ok, "entries": len(records), "error": error}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc", default=None, help="verify a single doc_id")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    chains = asyncio.run(_all_chains()) if not args.doc else {args.doc: asyncio.run(_all_chains()).get(args.doc, [])}
    if args.doc and not chains.get(args.doc):
        print(f"no audit entries for {args.doc}")
        return 0

    results = [_verify(doc_id, records) for doc_id, records in sorted(chains.items())]
    broken = [r for r in results if not r["ok"]]

    if args.json:
        print(json.dumps({"chains": len(results), "broken": broken}, indent=2))
        return 1 if broken else 0

    print(f"{len(results)} chain(s) checked, {len(broken)} broken")
    for r in results:
        mark = "OK " if r["ok"] else "BROKEN"
        print(f"  [{mark}] {r['doc_id']:<40} entries={r['entries']}" + (f"  {r['error']}" if r["error"] else ""))
    if broken:
        print("\nChains are broken — investigate before restore (see audit A-8).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
