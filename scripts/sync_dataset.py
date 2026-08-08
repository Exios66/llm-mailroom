#!/usr/bin/env python3
"""Build the Mailroom evaluation dataset in the connected Langfuse project.

Creates (or updates) the `mailroom-pilot` Langfuse dataset from
`examples/samples/manifest.csv`: one item per pilot sample, keyed by a
deterministic id (`mailroom-pilot-<sample_id>`) so re-runs upsert instead of
duplicating. Each item carries:

- `input`      — the document text (transcribed from the sample PDF via direct
                 parsing, no LLM) plus filename/matter id
- `expectedOutput` — the ground truth from the manifest (doc class + stage)
- `metadata`   — the full manifest row (source, license, size tier, notes)

This dataset is what experiments (prompt/model A/B runs) and judge calibration
run against. See docs/architecture.md (Evaluators & Quality).

Usage:
    python scripts/sync_dataset.py              # sync all samples
    python scripts/sync_dataset.py --dry-run    # preview without writing
    python scripts/sync_dataset.py --limit 5    # subset
    python scripts/sync_dataset.py --include contract
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.env import load_env  # noqa: E402

load_env()

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

from scripts.prepare_samples import prepare_samples  # noqa: E402

DATASET_NAME = "mailroom-pilot"
MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing) — cannot sync dataset.")
        return None
    return client


def _ensure_dataset(client) -> None:
    try:
        client.api.datasets.create(
            name=DATASET_NAME,
            description="Pilot evaluation set: 12 legal documents with ground-truth "
                        "doc class + stage from examples/samples/manifest.csv.",
            metadata={"source": "examples/samples/manifest.csv", "pipeline": "mailroom"},
        )
        print(f"Created dataset '{DATASET_NAME}'.")
    except Exception:
        existing = client.api.datasets.get(DATASET_NAME)
        print(f"Dataset '{DATASET_NAME}' already exists (id={existing.id}).")


def _doc_text(sample: dict, samples_dir: Path) -> str:
    from agents.pdf_transcriber import PDFTranscriber

    pdf = samples_dir / sample["subdir"] / sample["filename"]
    if not pdf.exists():
        logger.warning("sample_pdf_missing", path=str(pdf))
        return ""
    try:
        text, _ = PDFTranscriber()._extract_raw_text(pdf)
        return text or ""
    except Exception:
        logger.exception("sample_text_extract_failed", path=str(pdf))
        return ""


def sync_items(client, rows: list[dict], *, dry_run: bool, samples_dir: Path) -> int:
    synced = 0
    for row in rows:
        item_id = f"{DATASET_NAME}-{row['id']}"
        doc_text = _doc_text(row, samples_dir)
        if not doc_text.strip():
            logger.warning("skipping_empty_document", id=row["id"], filename=row["filename"])
            continue

        item_input = {
            "doc_text": doc_text,
            "filename": row["filename"],
            "matter_id": f"PILOT-{row['id']}",
        }
        expected_output = {
            "expected_doc_class": row["expected_doc_class"],
            "expected_stage": row["expected_stage"],
        }
        metadata = {
            "sample_id": row["id"],
            "subdir": row["subdir"],
            "filename": row["filename"],
            "size_tier": row["size_tier"],
            "source": row["source"],
            "license": row["license"],
            "notes": row["notes"],
        }

        if dry_run:
            print(f"would sync  {item_id}  ({row['expected_doc_class']}, chars={len(doc_text)})")
            synced += 1
            continue

        client.api.dataset_items.create(
            dataset_name=DATASET_NAME,
            id=item_id,
            input=item_input,
            expected_output=expected_output,
            metadata=metadata,
        )
        print(f"synced     {item_id}  ({row['expected_doc_class']}, chars={len(doc_text)})")
        synced += 1
    return synced


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync the pilot evaluation dataset to Langfuse.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing anything.")
    parser.add_argument("--limit", type=int, default=0, help="Only sync the first N samples (0 = all).")
    parser.add_argument("--include", help="Only sync samples of this expected doc class (e.g. contract).")
    args = parser.parse_args()

    client = _client()
    if client is None:
        return 1

    prepare_samples()
    samples_dir = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "samples"

    with MANIFEST.open() as fh:
        rows = list(csv.DictReader(fh))
    if args.include:
        rows = [r for r in rows if r["expected_doc_class"] == args.include]
    if args.limit:
        rows = rows[: args.limit]

    if not args.dry_run:
        _ensure_dataset(client)
    synced = sync_items(client, rows, dry_run=args.dry_run, samples_dir=samples_dir)

    if not args.dry_run:
        from langfuse import get_client

        get_client().flush()
    print(f"\n{len(rows)} sample(s) checked, {synced} {'would be' if args.dry_run else ''} synced to dataset '{DATASET_NAME}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
