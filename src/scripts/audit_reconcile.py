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

With ``--json`` stdout is the report and nothing else: log lines go to stderr
(the level and format still come from ``LOG_LEVEL`` / ``LOG_FORMAT``).

Exit status: 0 when nothing went wrong; 1 on a hash mismatch, a document write error, a sink whose
pending records could not be read (``sink_read_failed``: the report is incomplete, not clean), or
an unmigrated audit database (``AuditMigrationPending``: with ``--json`` a ``{"error": ...}`` object
on stdout). A spool file that does not exist is reported (``spool_missing``) but is not an error:
the spool is created by the first parked row, so a deployment that never parked one has none.
A document whose copy fails for a reason that will not change is moved to the spool's
``quarantined_rows`` table (kept, not deleted) and counted in ``quarantined``; later runs
no longer fail on it. Partial records and write-only copies may still need operator follow-up.
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing and keep the spool")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON on stdout (log lines go to stderr)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Reconcile pending records and print a report; return 1 on a hash or write error.

    ``--dry-run`` reports without writing or removing pending records.
    """
    args = _parser().parse_args(argv)

    from audit.models import AuditMigrationPending
    from audit.reconcile import reconcile

    try:
        report = asyncio.run(reconcile(dry_run=args.dry_run))
    except AuditMigrationPending as exc:
        message = f"audit migration has not run: {exc}"
        if args.json:
            print(json.dumps({"error": "audit_migration_pending", "message": message}, indent=2))
        else:
            print(f"audit-reconcile: {message}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        verb = "would write" if args.dry_run else "wrote"
        print(f"audit-reconcile: {verb} {report.recovered} recovered, {report.partial} partial, "
              f"{report.gap_closed} recovered_detail, {report.resumed} resumed, {report.failed} failed; skipped {report.skipped_existing} with a row; "
              f"refused {report.rejected_hash} on hash mismatch")
        if report.quarantined or report.quarantine_held:
            print(f"audit-reconcile: {report.quarantined} quarantined this run, {report.quarantine_held} held "
                  f"in quarantine (kept in the spool's quarantined_rows table)")
        if report.spool_missing:
            print(f"audit-reconcile: no spool at {', '.join(report.spool_missing)} (nothing parked, or its "
                  f"volume is not mounted)")
        if report.sink_read_failed:
            print(f"audit-reconcile: could not read {report.sink_read_failed} sink(s): "
                  f"{', '.join(report.sink_read_failed_names)}; this report is incomplete", file=sys.stderr)
    return 1 if report.rejected_hash or report.failed or report.sink_read_failed else 0


if __name__ == "__main__":
    # Only the script entry point re-points the logs: main() is also called in-process.
    if _parser().parse_known_args()[0].json:
        import structlog

        structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))  # level is kept
    sys.exit(main())
