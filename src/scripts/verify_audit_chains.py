#!/usr/bin/env python3
"""Verify the audit database (audit A-8, unified audit row spec §6).

Runs ``audit.verify.verify_audit_db``: the main chain, sub-rows, addenda, the
legacy per-event chains, trace I/O, and pending records (manifests with
``audit_written: pending`` and no row). Nonzero exit on any failure, so
restore/backup flows can gate on it.

Usage:
    python scripts/verify_audit_chains.py                 # full report
    python scripts/verify_audit_chains.py --doc d1        # one document's per-event chain only
    python scripts/verify_audit_chains.py --json          # machine-readable report
    python scripts/verify_audit_chains.py --check-sinks   # also check external trace sinks (roadmap M9)
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


async def _doc_chain(doc_id: str) -> dict:
    """One document's per-event chain (``--doc``), checked with today's algorithm."""
    from schemas.audit import AuditLogEntry, verify_legacy_chain
    from storage.audit_log import get_audit_chain
    from storage.db import ensure_schema

    ensure_schema()
    records = await get_audit_chain(doc_id)
    entries = [
        AuditLogEntry(
            entry_id=r["entry_id"], doc_id=doc_id, matter_id=r.get("matter_id", ""), event=r["event"],
            actor=r["actor"], detail=r["detail"], prev_hash=r["prev_hash"], entry_hash=r["entry_hash"],
            timestamp=r["timestamp"],
        )
        for r in records
    ]
    return {"doc_id": doc_id, "entries": len(entries), "ok": verify_legacy_chain(entries) if entries else True}


_LABELS = {
    "main": "main chain", "subrows": "sub-rows", "addenda": "addenda", "legacy": "legacy chains",
    "trace_io": "trace I/O", "pending": "pending records",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", default=None, help="verify one document's per-event chain only")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--check-sinks", action="store_true", help="also check external trace sinks")
    args = ap.parse_args()

    if args.doc:
        result = asyncio.run(_doc_chain(args.doc))
        if args.json:
            print(json.dumps(result, indent=2))
        elif not result["entries"]:
            print(f"no audit entries for {args.doc}")
        else:
            print(f"[{'OK ' if result['ok'] else 'BROKEN'}] {args.doc} entries={result['entries']}")
        return 0 if result["ok"] else 1

    from audit.verify import SECTIONS, verify_audit_db

    report = asyncio.run(verify_audit_db(check_sinks=args.check_sinks))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
        return 0 if report.ok else 1
    for name in SECTIONS:
        section = getattr(report, name)
        mark = "OK " if section.ok else "FAIL"
        print(f"  [{mark}] {_LABELS[name]:<16} checked={section.checked} failures={len(section.failures)}")
        for failure in section.failures[:20]:
            print(f"         {failure}")
        if len(section.failures) > 20:
            print(f"         ... {len(section.failures) - 20} more")
    if not report.ok:
        print("\nAudit verification failed; investigate before restore (see audit A-8).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
