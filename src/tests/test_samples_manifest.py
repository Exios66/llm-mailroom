import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
MANIFEST = ROOT / "docs" / "examples" / "samples" / "manifest.csv"

# docs/examples/ is a pruned heavy asset in the monorepo (sample PDFs +
# manifest). The upstream llm-mailroom repo is the reference for these.
pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(),
    reason="docs/examples/samples/manifest.csv absent (pruned heavy asset; see upstream repo)",
)

SOURCES_DIR = ROOT / "docs" / "examples" / "sources"
CUAD_DIR = ROOT / "docs" / "examples" / "samples" / "contract"
EXTERNAL_DIR = ROOT / "docs" / "examples" / "external"


def _rows():
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def test_manifest_has_rows_and_unique_ids():
    rows = _rows()
    assert len(rows) == 25
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate sample ids"
    for r in rows:
        assert r["filename"].endswith(".pdf")
        assert r.get("dataset") in ("original", "legalbench", "atticus")


def test_manifest_has_six_samples_per_external_source():
    from collections import Counter

    counts = Counter(r.get("dataset") or "original" for r in _rows())
    assert counts["original"] == 13
    assert counts["legalbench"] == 6
    assert counts["atticus"] == 6
    assert counts["pileoflaw"] == 0


def test_manifest_covers_insurance_claim_contrast():
    """Live-manifest insurance PDFs complement the local approved/denied/partial pack."""
    rows = [r for r in _rows() if r["expected_doc_class"] == "insurance_claim"]
    assert {r["id"] for r in rows} == {"insurance_01", "insurance_02", "insurance_03"}
    determinations = set()
    for r in rows:
        fields = json.loads(r["expected_fields"])
        determinations.add(fields["coverage_determination"])
        assert r["expected_stage"] == "archived"
        assert r.get("dataset") == "original"
        assert not r["source"].startswith(("CUAD", "external/"))
    assert determinations == {"approved", "denied", "partial"}


def test_manifest_expected_classes_are_valid_taxonomy():
    from pipeline.config import load_config

    valid = {c["key"] for c in load_config()["doc_classes"]}
    for r in _rows():
        assert r["expected_doc_class"] in valid, r["id"]


def test_manifest_expected_stages_valid():
    for r in _rows():
        assert r["expected_stage"] in ("archived", "review", "failed"), r["id"]


def test_manifest_has_schema_compatible_field_ground_truth():
    from schemas.documents import get_extraction_schema

    for row in _rows():
        raw = row.get("expected_fields", "").strip()
        assert raw, f"missing expected_fields: {row['id']}"
        fields = json.loads(raw)
        assert isinstance(fields, dict), row["id"]
        schema = get_extraction_schema(row["expected_doc_class"])
        assert schema is not None, row["id"]
        unknown = set(fields) - set(schema.model_fields)
        assert not unknown, f"unknown expected_fields for {row['id']}: {unknown}"


def test_manifest_referenced_sources_exist():
    for r in _rows():
        if r["source"].startswith("CUAD"):
            assert (CUAD_DIR / r["filename"]).exists(), f"missing CUAD pdf: {r['id']}"
        elif r["source"].startswith("external/"):
            assert (EXTERNAL_DIR / r["source"].removeprefix("external/")).exists(), (
                f"missing external source: {r['id']}"
            )
        else:
            assert (SOURCES_DIR / r["source"]).exists(), f"missing source: {r['id']}"


def test_manifest_committed_cuad_pdfs():
    # 3 original CUAD PDFs + 6 Atticus samples fetched by
    # scripts/fetch_external_samples.py.
    pdfs = list(CUAD_DIR.glob("*.pdf"))
    assert len(pdfs) == 9
    for p in pdfs:
        assert p.stat().st_size > 0


def test_retired_classes_are_absent_from_live_manifest():
    for r in _rows():
        assert r["expected_doc_class"] not in ("court_opinion", "due_diligence"), r["id"]
        assert (r.get("dataset") or "") != "pileoflaw"


def test_legalbench_maud_samples_are_merger_agreement_not_contract():
    """MAUD is its own class. CUAD Atticus rows stay contract."""
    for r in _rows():
        if r.get("dataset") == "legalbench":
            assert r["expected_doc_class"] == "merger_agreement", r["id"]
            assert r["subdir"] == "merger_agreement", r["id"]
        if r.get("dataset") == "atticus":
            assert r["expected_doc_class"] == "contract", r["id"]
        if r["id"].startswith("contract_"):
            assert r["expected_doc_class"] == "contract", r["id"]
