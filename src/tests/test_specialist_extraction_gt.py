"""Dedicated specialist scoring suites + post-hoc extraction GT."""

from observability.extraction_gt import build_expected_fields, catalog_expected_fields
from observability.posthoc_gt import extract_posthoc_fields
from observability.specialist_suites import (
    LIVE_EXTRACT_CLASSES,
    dedicated_suite,
    list_dedicated_suites,
    score_dedicated_suite,
    specialists_with_suites,
)
from pipeline.hf_corpora import example_rows, hub_sample
from observability.local_eval_packs import (
    compliance_local_samples,
    corporate_extraction_samples,
)


def test_every_live_specialist_has_a_dedicated_suite():
    suites = {row["doc_class"]: row for row in list_dedicated_suites()}
    assert set(suites) == set(LIVE_EXTRACT_CLASSES)
    mapping = specialists_with_suites()
    assert mapping["contracts_specialist"] == ["contract", "merger_agreement"]
    assert set(mapping) == {
        "contracts_specialist",
        "corporate_records_specialist",
        "correspondence_specialist",
        "compliance_specialist",
        "insurance_claims_specialist",
    }
    merger = dedicated_suite("merger_agreement")
    contract = dedicated_suite("contract")
    assert merger["specialist"] == contract["specialist"] == "contracts_specialist"
    assert merger["suite_key"] == "merger_agreement"
    assert "all_cash" in merger["subclasses"]
    assert "license" not in merger["subclasses"]
    assert "affiliate" in contract["subclasses"]
    for kind, row in suites.items():
        assert row["schema_fields"], kind
        assert row["field_types"], kind
        assert row["specialist"], kind


def test_dedicated_suite_scores_full_schema_not_just_subclass():
    expected = {
        "entity_name": "Meridian Holdings, Inc.",
        "record_type": "bylaws",
        "jurisdiction": "Delaware",
        "effective_date": "2023-02-01",
        "signatories": ["Thomas Meridian"],
    }
    result, extras = score_dedicated_suite("corporate_record", expected, expected)
    assert result.overall_score == 1.0
    assert len(result.field_scores) >= 4
    assert extras.get("extraction_f1") == 1.0

    claim = {
        "claim_number": "C-1",
        "insurer": "CMS Medicare",
        "insured_party": "Pat",
        "claim_type": "carrier",
        "coverage_determination": "denied",
        "denial_reasons": ["exclusion"],
        "claimed_amount": 10.0,
    }
    result, extras = score_dedicated_suite("insurance_claim", claim, claim)
    assert result.overall_score == 1.0
    assert extras["determination_consistency"] == 1.0


def test_posthoc_gt_fills_hub_examples_for_every_hf_class():
    by_class = {}
    for raw in example_rows():
        sample = hub_sample(raw)
        cls = sample["expected_hf_class"]
        if cls in by_class:
            continue
        fields, meta = build_expected_fields(sample)
        by_class[cls] = (fields, meta)
        catalog = catalog_expected_fields(sample)
        assert meta["n_fields"] >= 2, (cls, fields)
        assert meta["n_labeled"] >= 2, (cls, fields)
        assert meta["specialist"]
        # Post-hoc never overwrites a catalog/Hub value.
        for key, value in catalog.items():
            assert fields[key] == value, (cls, key)
    assert set(by_class) >= {
        "contract",
        "merger_agreement",
        "corporate_record",
        "correspondence",
        "insurance_claim",
    }
    cms, cms_meta = by_class["insurance_claim"]
    assert cms["claim_type"] in {"carrier", "inpatient", "outpatient", "pde"}
    assert cms.get("insurer")
    assert cms.get("claim_number")
    assert cms.get("coverage_determination") == "approved"
    assert cms_meta["n_posthoc"] >= 3

    corp, _ = by_class["corporate_record"]
    assert corp["record_type"]
    assert corp.get("entity_name") or corp.get("jurisdiction")

    mail, _ = by_class["correspondence"]
    assert mail["communication_type"]
    assert mail.get("sender") or mail.get("recipient") or mail.get("subject_matter") or mail.get("keywords")

    merger, _ = by_class["merger_agreement"]
    assert merger["merger_consideration"]
    assert merger.get("parties") or merger.get("document_name")

    contract, _ = by_class["contract"]
    assert contract["cuad_family"]
    assert contract.get("parties") or contract.get("document_name") or contract.get("effective_date")


def test_posthoc_gt_on_local_packs_matches_schema_keys():
    bylaws = corporate_extraction_samples()[0]
    fields, meta = build_expected_fields(bylaws)
    assert fields["entity_name"] == "Meridian Holdings, Inc."
    assert fields["record_type"] == "bylaws"
    assert fields["jurisdiction"] == "Delaware"
    assert meta["n_hub"] >= 5
    # Explicit pack labels win; post-hoc may add extras but not clobber.
    filing = compliance_local_samples()[0]
    fields, _ = build_expected_fields(filing)
    assert fields["filing_type"] == "10-K"
    assert fields["entity_name"] == "NovaTech Solutions, Inc."
    assert fields["regulatory_body"] == "SEC"


def test_posthoc_extractors_are_conservative_on_empty_text():
    assert extract_posthoc_fields("corporate_record", "") == {}
    assert extract_posthoc_fields("unknown", "hello") == {}
    fields, meta = build_expected_fields({
        "expected_hf_class": "corporate_record",
        "expected_subclass": "bylaws",
    })
    assert fields["record_type"] == "bylaws"
    assert meta["n_posthoc"] == 0
