#!/usr/bin/env python3
"""Reconcile processing/ claims orphaned by crashed processes (A-18/L-1).

A worker that dies between ``claim_file`` and the terminal move strands the
document in ``data/pipeline/processing/<worker_id>/`` forever. This script
finds those stale claims and either re-queues them to the inbox (they get
re-processed on the next watcher start) or retires them to ``failed/``.

Decision rule per file:
- no terminal manifest  → re-queue to the inbox (``--dry-run`` shows it)
- terminal manifest exists (archived/failed/review) → retire the copy to
  ``failed/`` (the terminal manifest stays authoritative)
- ``--move-all-to-failed`` forces the failed path for everything

Usage:
    python scripts/recover_processing.py                     # dry-run report
    python scripts/recover_processing.py --apply             # actually move
    python scripts/recover_processing.py --stale-minutes 15  # tighter cutoff
    python scripts/recover_processing.py --apply --move-all-to-failed
    python scripts/recover_processing.py --catalog           # dry-run: stale catalog rows
    python scripts/recover_processing.py --catalog --apply   # reconcile rows from manifests
"""

from __future__ import annotations

import argparse
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

from pipeline.bins import (  # noqa: E402
    list_stale_processing_files,
    mark_processing_dead,
    reconcile_stale_processing_file,
    terminal_manifest_for,
)


def _terminal_stages_from_manifests() -> dict[str, str]:
    """doc_id -> terminal stage, from the on-disk manifests (the authority —
    the same law the file reconciliation uses)."""
    from pipeline.bins import manifests_dir

    out: dict[str, str] = {}
    mdir = manifests_dir()
    if not mdir.exists():
        return out
    for mf in mdir.glob("*.json"):
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        doc_id = str(data.get("doc_id") or "")
        stage = str(data.get("stage") or "")
        if doc_id and stage in ("archived", "failed", "review"):
            out[doc_id] = stage
    return out


def reconcile_catalog_rows(*, apply: bool) -> int:
    """HUB-051 G4: catalog rows stranded in ``processing`` by dead watcher
    epochs. The conveyor position is read from the on-disk terminal manifest
    when one exists; a row whose manifest is terminal is reconciled to that
    stage so /ops/status and the relations clerk see the truth. Rows with no
    terminal manifest are reported only (a live claim may still be running —
    the file-mode pass owns those)."""
    import asyncio

    from storage.catalog import get_documents_by_stage, write_document_record

    rows = asyncio.run(get_documents_by_stage("processing"))
    if not rows:
        print("No catalog rows in processing stage.")
        return 0
    terminal = _terminal_stages_from_manifests()
    print(f"{len(rows)} catalog row(s) in processing stage:")
    n_fixed = 0
    for row in rows:
        stage = terminal.get(row.doc_id)
        if stage:
            print(f"  {row.doc_id} ({row.original_filename})  processing -> {stage}  (terminal manifest)")
            if apply:
                try:
                    asyncio.run(write_document_record({"doc_id": row.doc_id, "stage": stage}))
                    n_fixed += 1
                except Exception as exc:
                    print(f"    ERROR: {exc}", file=sys.stderr)
        else:
            print(f"  {row.doc_id} ({row.original_filename})  STALE — no terminal manifest (left untouched)")
    if not apply:
        print("\nDry-run — re-run with --apply to reconcile rows.")
    else:
        print(f"\nApplied: {n_fixed} row(s) reconciled from their terminal manifests.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry-run)")
    ap.add_argument("--stale-minutes", type=int, default=60, help="claim age cutoff (default: 60)")
    ap.add_argument("--move-all-to-failed", action="store_true",
                    help="retire every stale claim to failed/ instead of re-queueing")
    ap.add_argument("--catalog", action="store_true",
                    help="reconcile stale catalog PROCESSING rows from their terminal manifests (HUB-051)")
    args = ap.parse_args()

    if args.catalog:
        return reconcile_catalog_rows(apply=args.apply)

    stale = list_stale_processing_files(stale_minutes=args.stale_minutes)
    if not stale:
        print("No stale processing claims found.")
        return 0

    print(f"{len(stale)} stale processing claim(s):")
    n_requeue = n_fail = 0
    for f in stale:
        terminal = terminal_manifest_for(f.name)
        to_failed = args.move_all_to_failed or terminal
        action = "failed" if to_failed else "requeue"
        reason = "terminal manifest exists" if terminal else ("forced" if args.move_all_to_failed else "no terminal manifest")
        print(f"  {f}  -> {action}  ({reason})")
        if not args.apply:
            continue
        try:
            if args.move_all_to_failed:
                mark_processing_dead(f.parent.name, f.name)
                n_fail += 1
            else:
                action_done, _dest = reconcile_stale_processing_file(f)
                if action_done == "failed":
                    n_fail += 1
                else:
                    n_requeue += 1
            print(f"    done")
        except Exception as exc:
            print(f"    ERROR: {exc}", file=sys.stderr)

    if not args.apply:
        print("\nDry-run — re-run with --apply to move files.")
    else:
        print(f"\nApplied: {n_requeue} re-queued, {n_fail} retired to failed/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
