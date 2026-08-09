#!/usr/bin/env python3
"""Build the pilot sample PDF set, driven by examples/samples/manifest.csv.

Every row in the manifest is materialized under `data/samples/` (gitignored),
ready to drop into the pipeline inbox:

- Rows whose `source` points at a committed CUAD PDF
  (examples/samples/contract/...) are copied verbatim.
- Rows whose `source` points at a committed .txt under examples/sources/ are
  rendered to PDF with ReportLab.
- Rows whose `source` points at `external/...` (fetched by
  scripts/fetch_external_samples.py from LegalBench / Pile of Law) are also
  rendered to PDF with ReportLab.

The manifest is the source of truth: filenames, subdirectories, and ground-truth
expectations all live there.

Usage:
    python scripts/prepare_samples.py
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = REPO_ROOT / "examples" / "sources"
REAL_CONTRACTS_DIR = REPO_ROOT / "examples" / "samples" / "contract"
EXTERNAL_DIR = REPO_ROOT / "examples" / "external"
MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_pdf_from_text(source_txt: Path, dest_pdf: Path) -> None:
    dest_pdf.parent.mkdir(parents=True, exist_ok=True)

    body = ParagraphStyle(
        name="Body",
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        spaceAfter=6,
    )
    doc = SimpleDocTemplate(
        str(dest_pdf),
        pagesize=letter,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title=dest_pdf.stem,
    )

    story: list = []
    for block in source_txt.read_text(encoding="utf-8").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        story.append(Paragraph(_escape(block), body))
        story.append(Spacer(1, 4))
    doc.build(story)


def _load_manifest():
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def is_real_sample(row: dict) -> bool:
    """True when a manifest row is a real committed legal document.

    Real samples are either a CUAD/Atticus PDF committed under
    examples/samples/contract/ (source starts with "CUAD") or an external
    LegalBench / Pile of Law source committed under examples/external/ (source
    starts with "external/"). Everything else is a repo-written synthetic .txt
    under examples/sources/ that is rendered to PDF only for mock runs — it
    must never be processed by a real (non-mock) pilot run.
    """
    source = (row.get("source") or "").strip()
    return source.startswith("CUAD") or source.startswith("external/")


def prepare_samples(base_dir: Path | None = None) -> Path:
    """Materialize every manifest row under data/samples/. Returns its path."""
    if base_dir is None:
        base_dir = Path(os.environ.get("MAILROOM_BASE_DIR", "./data"))
    base_dir = Path(base_dir)
    samples_dir = base_dir / "samples"

    rows = _load_manifest()
    for row in rows:
        dest = samples_dir / row["subdir"] / row["filename"]
        dest.parent.mkdir(parents=True, exist_ok=True)

        source = row["source"]
        if source.startswith("CUAD"):
            # Real CUAD/Atticus PDFs committed under examples/samples/contract/
            # (the original 3 + the 6 fetched by fetch_external_samples.py).
            src = REAL_CONTRACTS_DIR / row["filename"]
            if not src.exists():
                raise SystemExit(f"Missing CUAD PDF: {src}")
            shutil.copyfile(src, dest)
        elif source.startswith("external/"):
            # Fetched dataset samples (LegalBench MAUD, Pile of Law): committed
            # .txt under examples/external/, rendered to PDF here.
            txt = EXTERNAL_DIR / source.removeprefix("external/")
            if not txt.exists():
                raise SystemExit(f"Missing external source text: {txt}")
            generate_pdf_from_text(txt, dest)
        else:
            txt = SOURCES_DIR / source
            if not txt.exists():
                raise SystemExit(f"Missing source text: {txt}")
            generate_pdf_from_text(txt, dest)

    # Validate every manifest row now has a materialized PDF
    missing = [f"{r['subdir']}/{r['filename']}" for r in rows
               if not (samples_dir / r["subdir"] / r["filename"]).exists()]
    if missing:
        raise SystemExit("Missing sample files:\n" + "\n".join(missing))

    print(f"Prepared {len(rows)} samples in {samples_dir}")
    return samples_dir


if __name__ == "__main__":
    prepare_samples()
