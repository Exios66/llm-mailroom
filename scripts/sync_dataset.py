#!/usr/bin/env python3
"""Build the Mailroom evaluation datasets in the connected Langfuse project.

Creates (or updates) a Langfuse dataset per source corpus from
`examples/samples/manifest.csv` — `mailroom-pilot` (original samples),
`mailroom-pilot-legalbench`, `mailroom-pilot-atticus`, `mailroom-pilot-pileoflaw`
— one item per sample, keyed by a deterministic id (`<dataset>-<sample_id>`) so
re-runs upsert instead of duplicating. Each item carries:

- `input`      — the document text (transcribed from the sample PDF via direct
                 parsing, no LLM) plus filename/matter id
- `expectedOutput` — the ground truth from the manifest (doc class + stage)
- `metadata`   — the full manifest row (source, license, size tier, dataset, notes)

This dataset is what experiments (prompt/model A/B runs) and judge calibration
run against. See docs/architecture.md (Evaluators & Quality).

Usage:
    python scripts/sync_dataset.py              # sync all sources (default)
    python scripts/sync_dataset.py --dry-run    # preview without writing
    python scripts/sync_dataset.py --limit 5    # subset
    python scripts/sync_dataset.py --include contract
    python scripts/sync_dataset.py --dataset pileoflaw
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

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

from scripts.prepare_samples import prepare_samples  # noqa: E402

DATASET_NAME = "mailroom-pilot"
MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"

# Per-source Langfuse datasets: each external corpus gets its own dataset so
# pilot results can be compared per source. `original` keeps the legacy name.
SOURCE_DATASETS = {
    "original": "mailroom-pilot",
    "legalbench": "mailroom-pilot-legalbench",
    "atticus": "mailroom-pilot-atticus",
    "pileoflaw": "mailroom-pilot-pileoflaw",
}
SOURCE_DESCRIPTIONS = {
    "original": "Pilot evaluation set: 12 legal documents with ground-truth "
                "doc class + stage from examples/samples/manifest.csv.",
    "legalbench": "LegalBench samples: 6 MAUD v1 merger agreements (the full "
                  "contract texts behind the maud_* tasks) — CC BY 4.0.",
    "atticus": "The Atticus Project samples: 6 CUAD v1 contract PDFs (SEC "
               "filing exhibits) — CC BY 4.0.",
    "pileoflaw": "Pile of Law samples: 6 U.S. court opinions (public-domain "
                 "subsets only) — court_opinion class.",
}


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing) — cannot sync dataset.")
        return None
    return client


def _ensure_dataset(client, dataset_name: str, source: str) -> None:
    try:
        client.api.datasets.create(
            name=dataset_name,
            description=SOURCE_DESCRIPTIONS.get(source, SOURCE_DESCRIPTIONS["original"]),
            metadata={"source": "examples/samples/manifest.csv", "pipeline": "mailroom"},
        )
        print(f"Created dataset '{dataset_name}'.")
    except Exception:
        existing = client.api.datasets.get(dataset_name)
        print(f"Dataset '{dataset_name}' already exists (id={existing.id}).")


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


def sync_items(client, rows: list[dict], *, dry_run: bool, samples_dir: Path, dataset_name: str) -> int:
    synced = 0
    for row in rows:
        item_id = f"{dataset_name}-{row['id']}"
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
            "dataset": row.get("dataset", ""),
        }

        if dry_run:
            print(f"would sync  {item_id}  ({row['expected_doc_class']}, chars={len(doc_text)})")
            synced += 1
            continue

        client.api.dataset_items.create(
            dataset_name=dataset_name,
            id=item_id,
            input=item_input,
            expected_output=expected_output,
            metadata=metadata,
        )
        print(f"synced     {item_id}  ({row['expected_doc_class']}, chars={len(doc_text)})")
        synced += 1
    return synced


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync the pilot evaluation dataset(s) to Langfuse.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing anything.")
    parser.add_argument("--limit", type=int, default=0, help="Only sync the first N samples (0 = all).")
    parser.add_argument("--include", help="Only sync samples of this expected doc class (e.g. contract).")
    parser.add_argument(
        "--dataset",
        choices=sorted(SOURCE_DATASETS),
        default=None,
        help="Only sync samples from this source corpus (default: all — every "
        "sample goes to its per-source dataset).",
    )
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
    if args.dataset:
        rows = [r for r in rows if (r.get("dataset") or "original") == args.dataset]
    if args.limit:
        rows = rows[: args.limit]

    # Group by source corpus; each gets its own Langfuse dataset.
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r.get("dataset") or "original", []).append(r)

    total_synced = 0
    for source, source_rows in sorted(by_source.items()):
        dataset_name = SOURCE_DATASETS[source]
        if not args.dry_run:
            _ensure_dataset(client, dataset_name, source)
        total_synced += sync_items(
            client,
            source_rows,
            dry_run=args.dry_run,
            samples_dir=samples_dir,
            dataset_name=dataset_name,
        )

    if not args.dry_run:
        from langfuse import get_client

        get_client().flush()
    print(
        f"\n{len(rows)} sample(s) checked, {total_synced} {'would be' if args.dry_run else ''} synced "
        f"to dataset(s): {', '.join(sorted(SOURCE_DATASETS[s] for s in by_source))}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
