"""sync_dataset --source hf: HF corpus rows → dataset items with pinned provenance."""

import json

from scripts import sync_dataset as sd

ROW = {
    "role": "insurance_claim",
    "doc_text": "CLAIM text",
    "labels": {
        "filename": "carrier:1.txt",
        "expected": "insurance_claim",
        "expected_stage": "archived",
        "expected_subclass": "carrier",
        "document_id": "DOC-1",
        "source_corpus": "cms_desynpuf",
        "content_sha256": "abc",
        "gt_fields": {"claim_number": "1", "not_a_schema_field": "x"},
    },
}
PROV = {"hub_sha": "v9.1", "dataset": "Lucius-Morningstar/mailroom-dataset"}


def test_hf_rows_map_to_manifest_shape_with_revision_pin():
    (row,) = sd.hf_manifest_rows([ROW], PROV)
    assert row["id"] == "DOC-1" and row["expected_doc_class"] == "insurance_claim"
    assert row["dataset"] == "hf" and row["revision"] == "v9.1"
    assert row["doc_text"] == "CLAIM text" and row["content_sha256"] == "abc"


def test_hf_gt_fields_are_clamped_to_the_live_schema():
    (row,) = sd.hf_manifest_rows([ROW], PROV)
    fields = json.loads(row["expected_fields"])
    assert fields == {"claim_number": "1"}


def test_hf_rows_without_a_live_class_are_dropped():
    bad = {**ROW, "labels": {**ROW["labels"], "expected": "court_opinion"}}
    assert sd.hf_manifest_rows([bad], PROV) == []


def test_doc_text_prefers_inline_text(tmp_path):
    assert sd._doc_text({"doc_text": "inline", "subdir": "x", "filename": "y"}, tmp_path) == "inline"


def test_hf_source_dataset_name_registered():
    assert sd.SOURCE_DATASETS["hf"] == "mailroom-hf"
