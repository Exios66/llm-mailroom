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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry-run)")
    ap.add_argument("--stale-minutes", type=int, default=60, help="claim age cutoff (default 60)")
    ap.add_argument("--move-all-to-failed", action="store_true",
                    help="retire every stale claim to failed/ instead of re-queueing")
    args = ap.parse_args()

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
