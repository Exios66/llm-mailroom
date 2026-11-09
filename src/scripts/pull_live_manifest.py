#!/usr/bin/env python3
"""Pull the live mailroom-dataset manifest into the pipeline sample shape.

Examples:
    PYTHONPATH=src python src/scripts/pull_live_manifest.py --live
    PYTHONPATH=src python src/scripts/pull_live_manifest.py --live --per-class 3 \
        --out data/live_manifest/smoke.csv
    PYTHONPATH=src python src/scripts/pull_live_manifest.py --check \
        src/tests/fixtures/live_manifest_sample.csv
    PYTHONPATH=src python src/scripts/pull_live_manifest.py --fixture

The output CSV is a manifest, never document text. Every row records the
exact Hub commit and content sha256 so a smoke run can fetch and verify the
same real document later.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.live_manifest import (  # noqa: E402
    CLASSES,
    DEFAULT_MAX_CHARS,
    MANIFEST_SCHEMA_VERSION,
    build_manifest_rows,
    load_frames,
    load_manifest,
    resolve_revision,
    select_smoke_rows,
    write_manifest_csv,
)

DEFAULT_FIXTURE = SRC_DIR / "tests" / "fixtures" / "live_manifest_sample.csv"


def _default_out(revision: dict) -> Path:
    label = revision.get("tag") or str(revision.get("sha") or "unknown")[:12]
    return SRC_DIR.parent / "data" / "live_manifest" / label / "manifest.csv"


def _classes(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="tag, branch or commit (default: newest semver tag)")
    parser.add_argument("--per-class", type=int, default=0, help="select N small archived docs per class")
    parser.add_argument("--classes", default="", help="comma-separated classes (default: all five)")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="selection ceiling, never truncation")
    parser.add_argument("--out", type=Path, help="output CSV (default data/live_manifest/<revision>/manifest.csv)")
    parser.add_argument("--fixture", action="store_true", help="write the committed 15-row smoke fixture")
    parser.add_argument("--check", type=Path, help="validate an existing manifest offline")
    parser.add_argument("--json", action="store_true", help="one JSON summary on stdout")
    args = parser.parse_args(argv)

    if args.check:
        try:
            rows = load_manifest(args.check)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        payload = {
            "ok": True,
            "manifest": str(args.check),
            "rows": len(rows),
            "classes": sorted({row["expected_doc_class"] for row in rows}),
            "schema_version": MANIFEST_SCHEMA_VERSION,
        }
        print(json.dumps(payload, indent=2) if not args.json else json.dumps(payload))
        return 0

    revision = resolve_revision(args.revision)
    gt, blind, provenance = load_frames(args.revision)
    rows, stats = build_manifest_rows(gt, blind, revision)
    if stats["integrity"]["mismatched"]:
        print(
            f"refusing: {stats['integrity']['mismatched']} content_sha256 mismatches",
            file=sys.stderr,
        )
        return 1

    selected = rows
    if args.per_class or args.fixture:
        classes = _classes(args.classes) or list(CLASSES)
        selected = select_smoke_rows(
            rows,
            per_class=args.per_class or 3,
            classes=classes,
            max_chars=args.max_chars,
        )
    out = args.out or (DEFAULT_FIXTURE if args.fixture else _default_out(revision))
    if args.fixture and not args.out:
        out = DEFAULT_FIXTURE
    payload = write_manifest_csv(
        selected,
        out,
        {
            **provenance,
            "stats": stats,
            "selection": {
                "per_class": args.per_class or (3 if args.fixture else None),
                "classes": _classes(args.classes) or None,
                "max_chars": args.max_chars,
                "rows": len(selected),
            },
        },
    )
    summary = {
        "ok": True,
        "manifest": str(out),
        "rows": len(selected),
        "total_rows": len(rows),
        "revision": revision,
        "csv_sha256": payload["csv_sha256"],
        "classes": stats["classes"],
        "integrity": stats["integrity"],
        "fixture": bool(args.fixture),
    }
    print(json.dumps(summary, indent=2) if not args.json else json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
