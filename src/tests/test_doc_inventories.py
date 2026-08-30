"""Hub inventories for non-contract specialists: normalize, enrich, handoff."""

from langchain_agents.doc_inventories import (
    CLAIM_TYPE_DESCRIPTION,
    COMMUNICATION_TYPE_DESCRIPTION,
    COMPLIANCE_FILING_TYPES,
    CORPORATE_RECORD_TYPES,
    CORRESPONDENCE_TYPES,
    FILING_TYPE_DESCRIPTION,
    INSURANCE_CLAIM_TYPES,
    RECORD_TYPE_DESCRIPTION,
    enrich_extraction,
    normalize_claim_type,
    normalize_communication_type,
    normalize_filing_type,
    normalize_record_type,
    skip_conflict_field,
    specialist_handoff,
)
from langchain_agents.specialist_agents import (
    COMPLIANCE_FILING_SCHEMA,
    CORPORATE_RECORDS_SCHEMA,
    CORRESPONDENCE_SCHEMA,
    INSURANCE_CLAIMS_SCHEMA,
)
from schemas.documents import (
    ComplianceFilingExtraction,
    CorporateRecordExtraction,
    CorrespondenceExtraction,
    InsuranceClaimExtraction,
)


def test_hub_token_lists_match_ground_truth():
    assert CORPORATE_RECORD_TYPES == (
        "articles_of_incorporation",
        "bylaws",
        "powers_of_attorney",
        "rights_instrument",
        "other",
    )
    assert "email" in CORRESPONDENCE_TYPES
    assert "attorney_demand" in CORRESPONDENCE_TYPES
    assert "meeting_request" in CORRESPONDENCE_TYPES
    assert INSURANCE_CLAIM_TYPES[:4] == ("pde", "inpatient", "outpatient", "carrier")
    assert "10-K" in COMPLIANCE_FILING_TYPES
    assert "S-1" in COMPLIANCE_FILING_TYPES


def test_normalize_record_type_hub_and_aliases():
    for token in CORPORATE_RECORD_TYPES:
        assert normalize_record_type(token) == token
    assert normalize_record_type("Certificate of Incorporation") == "articles_of_incorporation"
    assert normalize_record_type("Articles of Incorporation") == "articles_of_incorporation"
    assert normalize_record_type("Bylaws") == "bylaws"
    assert normalize_record_type("Power of Attorney") == "powers_of_attorney"
    assert normalize_record_type("specimen stock certificate") == "rights_instrument"
    assert normalize_record_type("Stockholder Rights Agreement") == "rights_instrument"


def test_normalize_communication_and_claim_and_filing():
    assert normalize_communication_type("Enron inbox email") == "email"
    assert normalize_communication_type("memorandum") == "memo"
    assert normalize_communication_type("Attorney Demand Letter") == "attorney_demand"
    assert normalize_communication_type("meeting invite") == "meeting_request"
    assert normalize_communication_type("demand") == "demand"
    assert normalize_claim_type("Part D Event") == "pde"
    assert normalize_claim_type("OUTPATIENT") == "outpatient"
    assert normalize_claim_type("hospital inpatient") == "inpatient"
    assert normalize_claim_type("carrier") == "carrier"
    assert normalize_claim_type("workers' compensation") == "workers_comp"
    assert normalize_filing_type("10-K annual report") == "10-K"
    assert normalize_filing_type("Form S-1") == "S-1"
    assert normalize_filing_type("DEF 14A") == "DEF 14A"


def test_enrich_fills_canonical_tokens_without_clobbering_contracts():
    corp = enrich_extraction(
        {"entity_name": "Acme", "record_type": "Certificate of Incorporation"},
        doc_type="corporate_record",
    )
    assert corp["record_type"] == "articles_of_incorporation"
    mail = enrich_extraction(
        {"sender": "Jane", "communication_type": "e-mail"},
        doc_type="correspondence",
    )
    assert mail["communication_type"] == "email"
    claim = enrich_extraction(
        {"insurer": "CMS", "claim_type": "PDE"},
        doc_type="insurance_claim",
    )
    assert claim["claim_type"] == "pde"
    merger = enrich_extraction(
        {"contract_value": "all cash"},
        doc_type="merger_agreement",
    )
    assert merger["merger_consideration"] == "all_cash"


def test_handoff_lists_each_specialist_inventory():
    corp = specialist_handoff("corporate_record")
    assert "articles_of_incorporation" in corp
    assert "rights_instrument" in corp
    mail = specialist_handoff("correspondence")
    assert "meeting_request" in mail
    assert "attorney_demand" in mail
    claim = specialist_handoff("insurance_claim")
    assert "pde" in claim
    assert "inpatient" in claim
    filing = specialist_handoff("compliance_filing")
    assert "10-K" in filing
    contract = specialist_handoff("contract", "license")
    assert "Anti-Assignment" in contract
    assert "license" in contract


def test_schemas_and_pydantic_carry_hub_descriptions():
    assert RECORD_TYPE_DESCRIPTION in CORPORATE_RECORDS_SCHEMA["properties"]["record_type"]["description"]
    assert COMMUNICATION_TYPE_DESCRIPTION in CORRESPONDENCE_SCHEMA["properties"]["communication_type"]["description"]
    assert FILING_TYPE_DESCRIPTION in COMPLIANCE_FILING_SCHEMA["properties"]["filing_type"]["description"]
    assert CLAIM_TYPE_DESCRIPTION in INSURANCE_CLAIMS_SCHEMA["properties"]["claim_type"]["description"]
    assert CorporateRecordExtraction.model_validate(
        {"record_type": "articles_of_incorporation"}
    ).record_type == "articles_of_incorporation"
    assert CorrespondenceExtraction.model_validate(
        {"communication_type": "email"}
    ).communication_type == "email"
    assert InsuranceClaimExtraction.model_validate(
        {"claim_type": "outpatient", "adjuster": None}
    ).claim_type == "outpatient"
    assert ComplianceFilingExtraction.model_validate(
        {"filing_type": "10-K"}
    ).filing_type == "10-K"


def test_skip_conflict_covers_type_and_clause_inventories():
    assert skip_conflict_field("record_type") is True
    assert skip_conflict_field("communication_type") is True
    assert skip_conflict_field("claim_type") is True
    assert skip_conflict_field("filing_type") is True
    assert skip_conflict_field("cuad_clauses") is True
    assert skip_conflict_field("parties") is False


def test_parse_hf_row_joins_insurance_ground_truth():
    from scripts.run_hf_pilot import expected_fields_for_sample, parse_hf_row

    row = parse_hf_row(
        {
            "filename": "cms_outpatient.txt",
            "doc_text": "DESYNPUF CLM_ID 123 outpatient claim table " + "x" * 300,
            "expected": "insurance_claim",
            "expected_subclass": "outpatient",
            "claim_number": "CLM-9",
            "insurer": "CMS Medicare",
            "claim_type": "outpatient",
            "claimed_amount": "440.00",
        }
    )
    assert row["expected_hf_class"] == "insurance_claim"
    assert row["claim_type"] == "outpatient"
    assert row["claim_number"] == "CLM-9"
    fields = expected_fields_for_sample(row)
    assert fields["claim_type"] == "outpatient"
    assert fields["insurer"] == "CMS Medicare"

    corp = expected_fields_for_sample({
        "expected_hf_class": "corporate_record",
        "expected_subclass": "articles_of_incorporation",
    })
    assert corp["record_type"] == "articles_of_incorporation"
    mail = expected_fields_for_sample({
        "expected_hf_class": "correspondence",
        "expected_subclass": "meeting_request",
    })
    assert mail["communication_type"] == "meeting_request"


def test_parse_hf_row_joins_corporate_schema_gt_when_present():
    from scripts.run_hf_pilot import expected_fields_for_sample, parse_hf_row

    row = parse_hf_row({
        "filename": "ex3-1.htm",
        "doc_text": "BYLAWS OF REVENUE.COM " + "x" * 300,
        "expected": "corporate_record",
        "expected_subclass": "bylaws",
        "entity_name": "Revenue.com Corporation",
        "jurisdiction": "Nevada",
        "key_provisions": '["annual meeting"]',
    })
    fields = expected_fields_for_sample(row)
    assert fields["record_type"] == "bylaws"
    assert fields["entity_name"] == "Revenue.com Corporation"
    assert fields["jurisdiction"] == "Nevada"
    assert fields.get("subject_matter") or fields.get("keywords")
    assert "annual meeting" in str(fields.get("subject_matter") or "") or any(
        "annual" in str(k).lower() for k in (fields.get("keywords") or [])
    )


def test_score_row_extraction_gates_homogeneous_insurance_gt():
    from scripts.run_hf_pilot import score_row_extraction

    expected = {
        "coverage_determination": "approved",
        "denial_reasons": [],
        "claimed_amount": 110.0,
        "claim_type": "carrier",
        "insurer": "CMS Medicare",
    }
    predicted = dict(expected)
    scored = score_row_extraction(predicted, expected, "insurance_claim")
    assert scored is not None
    assert scored["gt_homogeneity"] is True
    assert scored["determination_consistency_is_quality"] is False
    assert scored["determination_consistency"] == 1.0

    denied_exp = {
        "coverage_determination": "denied",
        "denial_reasons": ["lapse"],
        "claim_type": "auto",
        "insurer": "Acme",
    }
    denied_pred = dict(denied_exp)
    mixed = score_row_extraction(denied_pred, denied_exp, "insurance_claim")
    assert mixed is not None
    assert mixed.get("gt_homogeneity") is not True
    assert mixed.get("determination_consistency_is_quality") is not False
    assert mixed["determination_consistency"] == 1.0
    bad = score_row_extraction(
        {**denied_pred, "denial_reasons": []}, denied_exp, "insurance_claim"
    )
    assert bad["determination_consistency"] == 0.0
    from scripts.run_hf_pilot import expected_fields_for_sample, parse_hf_row

    row = parse_hf_row(
        {
            "filename": "cms_outpatient.txt",
            "doc_text": "DESYNPUF CLM_ID 123 outpatient claim table " + "x" * 300,
            "expected": "insurance_claim",
            "expected_subclass": "outpatient",
            "claim_number": "CLM-9",
            "insurer": "CMS Medicare",
            "claim_type": "outpatient",
            "claimed_amount": "440.00",
        }
    )
    assert row["expected_hf_class"] == "insurance_claim"
    assert row["claim_type"] == "outpatient"
    assert row["claim_number"] == "CLM-9"
    fields = expected_fields_for_sample(row)
    assert fields["claim_type"] == "outpatient"
    assert fields["insurer"] == "CMS Medicare"

    corp = expected_fields_for_sample({
        "expected_hf_class": "corporate_record",
        "expected_subclass": "articles_of_incorporation",
    })
    assert corp["record_type"] == "articles_of_incorporation"
    mail = expected_fields_for_sample({
        "expected_hf_class": "correspondence",
        "expected_subclass": "meeting_request",
    })
    assert mail["communication_type"] == "meeting_request"


def test_sorter_catalogs_come_from_dojo_without_replacing_hub_extract_tokens():
    from langchain_agents.doc_inventories import (
        CORPORATE_RECORD_TYPES,
        sorter_subclass_catalog,
        valid_sorter_subclasses,
        normalize_sorter_subclass,
        format_sorter_subclass_catalogs,
    )

    assert CORPORATE_RECORD_TYPES == (
        "articles_of_incorporation",
        "bylaws",
        "powers_of_attorney",
        "rights_instrument",
        "other",
    )
    corp_catalog = sorter_subclass_catalog("corporate_record")
    assert "certificate_of_formation" in corp_catalog
    assert "board_resolution" in corp_catalog
    assert "bylaws" in corp_catalog
    merger = sorter_subclass_catalog("merger_agreement")
    assert merger == (
        "all_cash",
        "all_stock",
        "mixed_cash_stock",
        "mixed_cash_stock_election",
        "other",
    )
    assert "license" not in merger
    assert sorter_subclass_catalog("due_diligence") == ()
    assert sorter_subclass_catalog("court_opinion") == ()
    assert normalize_sorter_subclass("correspondence", "Email") == "email"
    assert normalize_sorter_subclass("insurance_claim", "PDE") == "pde"
    assert "auto" in valid_sorter_subclasses("insurance_claim")
    text = format_sorter_subclass_catalogs()
    assert "content_topic" in text
    assert "merger_agreement" in text
    assert "10-K" in text
