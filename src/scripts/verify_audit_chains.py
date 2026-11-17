#!/usr/bin/env python3
"""Verify the hash-chained audit storage (audit A-8; roadmap M4).

Two modes:

* default — the full :func:`audit.verify.verify_audit_db` report: the unified
  main chain, attempt sub-rows, addenda, the legacy per-document chain and
  stored trace-I/O hashes. Pending rows/sinks are reported as *uncollected*,
  never as proof.
* ``--doc ID`` — one document: its per-event chain (legacy history after an
  explicit migration) AND its unified row, sub-rows and addenda, checked with the
  full verifier's own rules scoped to that document.

Nonzero exit when anything checked is broken — so restore/backup flows can gate
on it. Manifests parked as ``pending`` / ``pending_partial`` are never proof and
are not failures by default: they are listed in a warning (and as
``pending_manifests`` in ``--json``), and ``--fail-on-pending`` makes them exit 1
for flows that must not restore past an unreconciled gap
(``audit_reconcile.py`` closes them). In the default full-report mode the verification reads run in one
read-only snapshot; ``--doc ID`` reads the legacy per-document chains in
separate statements and makes no single-snapshot guarantee. The readers call
``ensure_schema()``, which may create missing tables and, only with
``MAILROOM_AUDIT_MIGRATE=1``, run the opt-in migration; the verifier never
migrates by itself.

Usage:
    python scripts/verify_audit_chains.py                 # full report
    python scripts/verify_audit_chains.py --json          # machine-readable
    python scripts/verify_audit_chains.py --check-sinks   # mark sink checks
    python scripts/verify_audit_chains.py --doc d1        # single doc
    python scripts/verify_audit_chains.py --fail-on-pending
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


#: Report section names as the operator-facing labels (PR #160's wording).
#: ``SECTIONS`` (the order) comes from ``audit.verify``, the single source.
_LABELS = {
    "main": "main chain",
    "subrows": "sub-rows",
    "addenda": "addenda",
    "legacy": "legacy chains",
    "trace_io": "trace I/O",
    "pending": "pending records",
}


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


def _pending_warning(count: int, *, fail: bool, doc: str | None = None, state: str | None = None) -> str:
    """The warning for manifests parked ``pending`` / ``pending_partial`` (never verified, never proof)."""
    if doc and state == "unavailable":
        what = f"the pending state of {doc} could not be read (unreadable manifest)"
    else:
        what = f"{doc} is parked as {state}" if doc else f"{count} manifest(s) are parked as pending / pending_partial"
    tail = "failing (--fail-on-pending)" if fail else "not a failure unless --fail-on-pending is given"
    return (f"\nWARNING: {what}: the row exists only in a sink, so this verification proves nothing for it. "
            f"Run scripts/audit_reconcile.py before restoring ({tail}).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc", default=None, help="verify a single doc_id's per-event chain")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--check-sinks",
        action="store_true",
        help="also ask for external-sink checks (the offline verifier reports them uncollected)",
    )
    ap.add_argument(
        "--fail-on-pending",
        action="store_true",
        help="exit 1 when any manifest is parked as pending / pending_partial (default: warn, keep the exit status)",
    )
    args = ap.parse_args()

    if not args.doc:
        from audit.readers import _pending_scan
        from audit.verify import SECTIONS, verify_audit_db

        report = asyncio.run(verify_audit_db(check_sinks=args.check_sinks))
        pending, unreadable = _pending_scan()
        fail_pending = bool((pending or unreadable) and args.fail_on_pending)
        if args.json:
            print(json.dumps({**report.as_dict(), "pending_manifests": pending,
                              "unreadable_manifests": unreadable}, indent=2, default=str))
            return 0 if report.ok and not fail_pending else 1
        print("audit verification:" + ("" if report.migrated else " (migration pending)"))
        for name in SECTIONS:
            section = getattr(report, name)
            state = "OK " if section.ok else "BROKEN"
            collected = "" if section.collected else "  [not collected]"
            print(
                f"  [{state}] {_LABELS[name]:<16} checked={section.checked} "
                f"failures={len(section.failures)}{collected}"
            )
            if section.note:
                print(f"           {section.note}")
            for failure in section.failures[:10]:
                print(f"           {failure}")
        if pending:
            print(_pending_warning(pending, fail=args.fail_on_pending))
        if unreadable:
            tail = "failing (--fail-on-pending)" if args.fail_on_pending else "not a failure unless --fail-on-pending is given"
            print(f"\nWARNING: {unreadable} manifest(s) could not be read, so their pending state is unknown ({tail}).")
        if not report.ok:
            print("\nAudit storage is broken — investigate before restore (see audit A-8).")
            return 1
        return 1 if fail_pending else 0

    from audit.readers import _pending
    from audit.verify import verify_document
    from storage.audit_log import get_audit_chain
    from storage.db import ensure_schema

    ensure_schema()
    # One query for the requested document; it reads the legacy table after a migration.
    chains = {args.doc: asyncio.run(get_audit_chain(args.doc))}
    unified = asyncio.run(verify_document(args.doc))
    parked = _pending(args.doc)
    # A failed unified check (e.g. addenda with no row) is a finding, never "no entries".
    if not chains.get(args.doc) and unified.main.checked == 0 and unified.ok and not parked:
        print(f"no audit entries for {args.doc}")
        return 0

    results = [_verify(doc_id, records) for doc_id, records in sorted(chains.items())]
    broken = [r for r in results if not r["ok"]]
    bad = bool(broken) or not unified.ok or bool(parked and args.fail_on_pending)

    if args.json:
        print(json.dumps({"chains": len(results), "broken": broken, "unified": unified.as_dict(),
                          "pending": parked}, indent=2, default=str))
        return 1 if bad else 0

    print(f"{len(results)} chain(s) checked, {len(broken)} broken")
    for r in results:
        mark = "OK " if r["ok"] else "BROKEN"
        print(f"  [{mark}] {r['doc_id']:<40} entries={r['entries']}" + (f"  {r['error']}" if r["error"] else ""))
    if not unified.migrated:
        print("  [--] unified row: migration pending, no unified row to check")
    else:
        for name in ("main", "subrows", "addenda"):
            section = getattr(unified, name)
            state = "OK " if section.ok else "BROKEN"
            print(f"  [{state}] {args.doc:<40} unified {_LABELS[name]}: checked={section.checked} "
                  f"failures={len(section.failures)}")
            for failure in section.failures[:10]:
                print(f"           {failure}")
    if parked:
        print(_pending_warning(1, fail=args.fail_on_pending, doc=args.doc, state=parked["audit_written"]))
    if broken or not unified.ok:
        print("\nChains are broken — investigate before restore (see audit A-8).")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
