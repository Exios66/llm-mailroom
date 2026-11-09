#!/usr/bin/env python3
"""Verify the hash-chained audit storage (audit A-8; roadmap M4).

Two modes:

* default — the full :func:`audit.verify.verify_audit_db` report: the unified
  main chain, attempt sub-rows, addenda, the legacy per-document chain and
  stored trace-I/O hashes. Pending rows/sinks are reported as *uncollected*,
  never as proof.
* ``--doc ID`` — today's per-document per-event chain (legacy history after an
  explicit migration).

Nonzero exit when anything checked is broken — so restore/backup flows can gate
on it. Verification reads run in one read-only snapshot. The explicit
``ensure_schema()`` at startup may create missing tables and, only with
``MAILROOM_AUDIT_MIGRATE=1``, run the opt-in migration; the verifier never
migrates by itself.

Usage:
    python scripts/verify_audit_chains.py                 # full report
    python scripts/verify_audit_chains.py --json          # machine-readable
    python scripts/verify_audit_chains.py --check-sinks   # mark sink checks
    python scripts/verify_audit_chains.py --doc d1        # single doc
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
    """Return per-event chains keyed by doc id, reading legacy history after migration."""
    from sqlalchemy import select, distinct
    from storage.audit_log import event_log_model, get_audit_chain

    ensure_schema()
    from storage.db import async_session

    async with async_session() as session:
        Model = await event_log_model(session)
        rows = await session.execute(select(distinct(Model.doc_id)))
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
    ap.add_argument("--doc", default=None, help="verify a single doc_id's per-event chain")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--check-sinks",
        action="store_true",
        help="request external-sink checks (reported as uncollected until roadmap M9)",
    )
    args = ap.parse_args()

    if not args.doc:
        from audit.verify import verify_audit_db

        report = asyncio.run(verify_audit_db(check_sinks=args.check_sinks))
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
            return 0 if report.ok else 1
        sections = ("main", "subrows", "addenda", "legacy", "trace_io", "pending")
        print("audit verification:" + ("" if report.migrated else " (migration pending)"))
        for name in sections:
            section = getattr(report, name)
            state = "OK " if section.ok else "BROKEN"
            collected = "" if section.collected else "  [not collected]"
            print(
                f"  [{state}] {name:<9} checked={section.checked} "
                f"failures={len(section.failures)}{collected}"
            )
            if section.note:
                print(f"           {section.note}")
            for failure in section.failures[:10]:
                print(f"           {failure}")
        if not report.ok:
            print("\nAudit storage is broken — investigate before restore (see audit A-8).")
            return 1
        return 0

    chains = {args.doc: asyncio.run(_all_chains()).get(args.doc, [])}
    if not chains.get(args.doc):
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
