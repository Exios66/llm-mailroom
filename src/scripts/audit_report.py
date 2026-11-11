#!/usr/bin/env python3
"""Write the audit report: one Markdown section per live document type (unified audit row spec §7).

Each section names the extraction schema, n, the extraction model and its
route (API or Modal), then F1, F2 and every other collected metric. A metric
no document of the type collected reads ``not collected``.

Usage:
    python src/scripts/audit_report.py                        # from the audit DB, to stdout
    python src/scripts/audit_report.py --parquet data/warehouse  # from the warehouse Parquet files
    python src/scripts/audit_report.py --out report.md        # write to a file
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

load_env()
from pipeline.logging import setup_logging  # noqa: E402

setup_logging()


def main(argv: list[str] | None = None) -> int:
    """Render the audit report from the database or Parquet to stdout or a file.

    Return 0 after writing; read and write errors propagate.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--parquet", type=Path, default=None,
                        help="read the audit_rows_<date>.parquet files in this directory instead of the DB")
    parser.add_argument("--out", type=Path, default=None, help="write the report here instead of stdout")
    args = parser.parse_args(argv)

    from audit.report import render_report

    report = asyncio.run(render_report(parquet_dir=args.parquet))
    if args.out is not None:
        args.out.write_text(report)
        print(f"audit-report: wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
