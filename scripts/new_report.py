#!/usr/bin/env python3
"""Scaffold a new evaluation write-up / audit / report under docs/reports/.

Every evaluation write-up, audit, or report lives in its own dedicated
subdirectory under ``docs/reports/`` (see docs/reports/README.md):

    audits/       repository audits and synthesis reports
    pilots/       pilot-run evaluation write-ups (vision tradeoffs, …)
    evaluations/  offline judge/quality evaluations over corpora

Usage:
    python scripts/new_report.py <kind> "TITLE" [--date YYYY-MM-DD] [--dry-run]

Examples:
    python scripts/new_report.py audits "Repo audit 2026"
    python scripts/new_report.py pilots "Vision tradeoff" --date 2026-08-10
    python scripts/new_report.py evaluations "CUAD subclass sweep"
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "docs" / "reports"
KINDS = {
    "audits": "Repository audit and synthesis reports",
    "pilots": "Pilot-run evaluation write-ups",
    "evaluations": "Offline judge/quality evaluations over corpora",
}


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "report"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", choices=sorted(KINDS), help="report subdirectory under docs/reports/")
    ap.add_argument("title", help="report title (used for the filename slug and H1)")
    ap.add_argument("--date", default=None, help="date for filename (default: today, YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="print the target path without writing")
    args = ap.parse_args()

    date = args.date or datetime.date.today().isoformat()
    target = REPORTS_DIR / args.kind / f"{date}-{_slug(args.title)}.md"

    if args.dry_run:
        print(target)
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        print(f"already exists: {target.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1

    header = f"""# {args.title}

- **Date**: {date}
- **Kind**: {args.kind} ({KINDS[args.kind]})
- **Status**: draft

## Scope

<!-- What was evaluated / audited / reported. -->

## Method

<!-- How: corpus, samples, commands run (scripts/…), thresholds, config. -->

## Findings

<!-- Numbered findings with evidence. -->

## Recommendations

<!-- Actionable next steps. -->
"""
    target.write_text(header)
    print(f"created: {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
