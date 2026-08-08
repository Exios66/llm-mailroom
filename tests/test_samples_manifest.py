import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "examples" / "samples" / "manifest.csv"
SOURCES_DIR = ROOT / "examples" / "sources"
CUAD_DIR = ROOT / "examples" / "samples" / "contract"


def _rows():
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def test_manifest_has_rows_and_unique_ids():
    rows = _rows()
    assert len(rows) >= 10
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate sample ids"
    for r in rows:
        assert r["filename"].endswith(".pdf")


def test_manifest_expected_classes_are_valid_taxonomy():
    from pipeline.config import load_config

    valid = {c["key"] for c in load_config()["doc_classes"]}
    for r in _rows():
        assert r["expected_doc_class"] in valid, r["id"]


def test_manifest_expected_stages_valid():
    for r in _rows():
        assert r["expected_stage"] in ("archived", "review", "failed"), r["id"]


def test_manifest_referenced_sources_exist():
    for r in _rows():
        if r["source"].startswith("CUAD"):
            assert (CUAD_DIR / r["filename"]).exists(), f"missing CUAD pdf: {r['id']}"
        else:
            assert (SOURCES_DIR / r["source"]).exists(), f"missing source: {r['id']}"


def test_manifest_contracts_are_committed_pdfs():
    pdfs = list(CUAD_DIR.glob("*.pdf"))
    assert len(pdfs) == 3
    for p in pdfs:
        assert p.stat().st_size > 0
