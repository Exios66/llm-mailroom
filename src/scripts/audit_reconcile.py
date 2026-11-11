#!/usr/bin/env python3
"""Write the audit rows that are waiting in the trace sinks (unified audit row spec §5.6 step 3).

A terminal row that could not be committed after three attempts is parked in
every trace sink (``audit_written: pending`` on the manifest). This reads them
back, refuses any copy whose hashes do not match, and writes each document's
row once with ``detail.recovered_from``. Run it after the database (or the
audit migration) is back; it is safe to run repeatedly.

Usage:
    python src/scripts/audit_reconcile.py             # write the waiting rows
    python src/scripts/audit_reconcile.py --dry-run   # report what would be written
    python src/scripts/audit_reconcile.py --json      # machine-readable report

Exit status: 0 when every copy verified, 1 when any copy was refused for a hash mismatch.
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


def main(argv: list[str] | None = None) -> int:
    """Reconcile pending records and print a report; return 1 on a hash mismatch.

    ``--dry-run`` reports without writing or removing pending records.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing and keep the spool")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    from audit.reconcile import reconcile

    report = asyncio.run(reconcile(dry_run=args.dry_run))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        verb = "would write" if args.dry_run else "wrote"
        print(f"audit-reconcile: {verb} {report.recovered} recovered, {report.partial} partial, "
              f"{report.gap_closed} recovered_detail; skipped {report.skipped_existing} with a row; "
              f"refused {report.rejected_hash} on hash mismatch")
    return 1 if report.rejected_hash else 0


if __name__ == "__main__":
    sys.exit(main())
