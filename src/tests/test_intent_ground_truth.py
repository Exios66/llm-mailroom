"""Purpose/gist ground truth — intent / subject_matter / keywords.

The plan (KANBAN-07x) adds three purpose labels to the corporate_record,
correspondence, and insurance_claim extraction + ground truth so successful
runs are graded against a closed intent vocabulary, a grounded one-sentence
subject, and <=8 text-grounded keywords. These tests pin: schema surface,
taxonomy field types, Hub GT-key joins, the controlled vocabulary, and the
manifest values derived from the underlying source documents.
"""

import csv
import json
from pathlib import Path

import pytest

from langchain_agents.doc_inventories import (
    CORPORATE_GT_KEYS,
    CORRESPONDENCE_GT_KEYS,
    INSURANCE_GT_KEYS,
    INTENT_LABELS,
    normalize_intent,
)
from schemas.documents import (
    CorporateRecordExtraction,
    CorrespondenceExtraction,
    InsuranceClaimExtraction,
)

LABELED = ("corporate_record", "correspondence", "insurance_claim")

MANIFEST = Path(__file__).resolve().parent.parent.parent / "docs/examples/samples/manifest.csv"

# docs/examples/ is a pruned heavy asset in the monorepo (sample PDFs +
# manifest). The upstream llm-mailroom repo is the reference for these.
pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file(),
    reason="docs/examples/samples/manifest.csv absent (pruned heavy asset; see upstream repo)",
)



def _manifest_rows():
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def test_schemas_include_purpose_gist_dimension():
    for schema in (CorporateRecordExtraction, CorrespondenceExtraction, InsuranceClaimExtraction):
        fields = schema.model_fields
        assert "intent" in fields
        assert "subject_matter" in fields
        assert "keywords" in fields


def test_taxonomy_field_types_include_purpose_gist():
    import yaml

    tax = yaml.safe_load((Path(__file__).resolve().parent.parent.parent / "src/config/taxonomy.yaml").read_text())
    classes = {c["key"]: c for c in tax["doc_classes"]}
    for key in LABELED:
        ft = classes[key]["field_types"]
        assert ft["intent"] == "name"
        assert ft["subject_matter"] == "free_text"
        assert ft["keywords"] in ("entity_list:name", "entity_list:free_text")


def test_hub_gt_keys_include_purpose_gist():
    assert {"intent", "subject_matter", "keywords"} <= set(CORPORATE_GT_KEYS)
    assert {"intent", "subject_matter", "keywords"} <= set(CORRESPONDENCE_GT_KEYS)
    assert {"intent", "subject_matter", "keywords"} <= set(INSURANCE_GT_KEYS)


def test_intent_controlled_vocabulary_and_normalization():
    for cls in LABELED:
        labels = INTENT_LABELS[cls]
        assert len(labels) >= 2
        assert labels[-1] == "other"
        assert len(set(labels)) == len(labels)
    assert normalize_intent("correspondence", "demand letter") == "payment_demand"
    assert normalize_intent("correspondence", "attorney demand") == "payment_demand"
    assert normalize_intent("corporate_record", "bylaws") == "governance_rules"
    assert normalize_intent("corporate_record", "board resolution") == "corporate_action_approval"
    assert normalize_intent("insurance_claim", "claim approved") == "coverage_determination"
    assert normalize_intent("insurance_claim", "initial fnol") == "claim_filing"
    assert normalize_intent("correspondence", "totally made up purpose") == ""
    assert normalize_intent("contract", "anything") == ""
    assert normalize_intent(None, "anything") == ""


def test_manifest_purpose_gist_labels_are_grounded():
    schema_for = {
        "corporate_record": CorporateRecordExtraction,
        "correspondence": CorrespondenceExtraction,
        "insurance_claim": InsuranceClaimExtraction,
    }
    labeled_rows = [
        r for r in _manifest_rows()
        if r["expected_doc_class"] in LABELED and r.get("expected_fields")
    ]
    assert len(labeled_rows) >= 7  # corporate_01/02 + correspondence_01/02 + insurance_01..03
    for row in labeled_rows:
        fields = json.loads(row["expected_fields"])
        cls = row["expected_doc_class"]
        assert set(fields) <= set(schema_for[cls].model_fields), row["id"]
        intent = fields["intent"]
        assert intent in INTENT_LABELS[cls], f"{row['id']}: intent {intent!r}"
        subject = fields["subject_matter"]
        assert isinstance(subject, str) and len(subject) >= 20, row["id"]
        assert "\n" not in subject and subject.endswith("."), row["id"]
        assert len(subject) <= 240, row["id"]
        keywords = fields["keywords"]
        assert isinstance(keywords, list) and 1 <= len(keywords) <= 8, row["id"]
        assert all(isinstance(k, str) and k.strip() for k in keywords), row["id"]


def test_intent_ground_truth_sync_contract_checks():
    from scripts.sync_hf_ground_truth import check_contract

    assert check_contract() == 0