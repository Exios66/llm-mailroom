"""Insurance CMS wiring against the Dojo insurance schema (llm-dojo-scoring#93).

On a CMS/DE-SynPUF row the Dojo scores ``claim_type`` as the product line (``health``),
``cms_source_table`` as its own label derived from ``expected_subclass``, and
``supporting_documents`` as identifier references (``score_cms_doc_ref``). These tests pin
the pipeline side of each: the derived table, the doc-ref guidance the model sees, and
the regex fill that the extract node and post-hoc GT share.
"""

import pytest
from llm_dojo_scoring.schemas.corpus import cms_source_table as dojo_cms_source_table
from llm_dojo_scoring.scoring.field_scoring import CMS_CLAIM_TYPES, score_cms_doc_ref

from langchain_agents.doc_inventories import (
    CMS_CLAIM_TABLES,
    SUPPORTING_DOCUMENTS_DESCRIPTION,
    cms_source_table_for,
    enrich_extraction,
    skip_conflict_field,
    specialist_handoff,
)
from langchain_agents.specialist_agents import INSURANCE_CLAIMS_SCHEMA
from observability.extraction_gt import catalog_expected_fields
from observability.posthoc_gt import extract_insurance_fields
from observability.specialist_suites import schema_fields

# Layouts follow the published DE-SynPUF notice templates; values are invented.
PART_D = """MEDICARE PRESCRIPTION DRUG BENEFIT -- PHARMACY STATEMENT (Part D)
Fill Reference: 100000000000001

Insurer:                  CMS Medicare Part D
Line of business:         health

PRESCRIPTION DETAILS
  Date of service:          2009-07-11
  Drug product (NDC):       00703051002
  Quantity dispensed:       30.0

COST SHARE
  Total drug cost:          $0.00
"""

CARRIER = """MEDICARE SUMMARY NOTICE -- PHYSICIAN/SUPPLIER CLAIM (Part B)
Notice ID: 100000000000002

SERVICE DETAILS
  Performing physician NPI: 5040521237

ITEMIZED SERVICE LINES (2)
   1. HCPCS 90816   allowed $60.00   paid $30.00
   2. HCPCS G0283   allowed $20.00   paid $10.00
"""

CARRIER_NO_CODES = """MEDICARE SUMMARY NOTICE -- PHYSICIAN/SUPPLIER CLAIM (Part B)
Notice ID: 100000000000003

SERVICE DETAILS
  Performing physician NPI: 8284740037

ITEMIZED SERVICE LINES (1)
   1. HCPCS ----   allowed $20.00   paid $40.00
"""

OUTPATIENT = """MEDICARE SUMMARY NOTICE -- OUTPATIENT CLAIM (Part B)
Notice ID: 100000000000004

  Facility provider number: 1800VN
  Treating NPIs:            3387884527

BILLING CODES
  Diagnoses (ICD-9):        71620, 71596
  Services (HCPCS):         97110, 97110, G0283
"""


def _refs(text: str) -> list[str]:
    return extract_insurance_fields(text).get("supporting_documents") or []


def _matches(pred: list[str], gold: list[str]) -> bool:
    return len(pred) == len(gold) and all(
        any(score_cms_doc_ref(p, g) == 1.0 for p in pred) for g in gold
    )


def test_part_d_fill_emits_the_ndc_reference_the_dojo_scores():
    refs = _refs(PART_D)
    assert refs == ["NDC 00703051002"]
    assert _matches(refs, ["NDC 00703051002"])


def test_cms_fill_emits_hcpcs_detail_only_for_real_codes():
    assert _matches(_refs(CARRIER), ["provider NPI 5040521237", "HCPCS detail (2 codes)"])
    assert _refs(CARRIER_NO_CODES) == ["provider NPI 8284740037"]
    refs = _refs(OUTPATIENT)
    assert refs == ["facility provider 1800VN", "provider NPI 3387884527", "HCPCS detail (3 codes)"]


def test_hcpcs_heading_words_are_not_counted_as_codes():
    from observability.posthoc_gt import _hcpcs_codes

    # A five-letter heading word is not a code (it used to give "HCPCS detail (2 codes)").
    assert _hcpcs_codes("HCPCS CODES\n   1. HCPCS 90816   allowed $60.00") == ["90816"]
    assert _hcpcs_codes("Services (HCPCS): CODES, J1234, G0283") == ["J1234", "G0283"]
    heading_only = CARRIER_NO_CODES.replace("HCPCS ----", "HCPCS CODES")
    assert _refs(heading_only) == ["provider NPI 8284740037"]
    assert _hcpcs_codes("HCPCS ----") == []
    # CPT Category III / PLA codes end in a letter and are still codes.
    assert _hcpcs_codes("HCPCS 0075T\nHCPCS 0001U") == ["0075T", "0001U"]
    assert _hcpcs_codes("Services (HCPCS): 0075T, 90816") == ["0075T", "90816"]


def test_supporting_documents_guidance_names_every_cms_reference_form():
    spec = INSURANCE_CLAIMS_SCHEMA["properties"]["supporting_documents"]
    assert spec["description"] == SUPPORTING_DOCUMENTS_DESCRIPTION
    note = specialist_handoff("insurance_claim")
    for text in (SUPPORTING_DOCUMENTS_DESCRIPTION, note):
        for form in ("facility provider", "provider NPI", "HCPCS detail", "NDC"):
            assert form in text, (form, text)


def test_cms_source_table_is_never_asked_of_the_model():
    assert "cms_source_table" not in INSURANCE_CLAIMS_SCHEMA["properties"]
    assert "cms_source_table" not in INSURANCE_CLAIMS_SCHEMA.get("required", [])
    # The gate and the completeness judge read schema_fields: they see the derived field.
    assert "cms_source_table" in schema_fields("insurance_claim")
    assert skip_conflict_field("cms_source_table")


@pytest.mark.parametrize("table", CMS_CLAIM_TABLES)
def test_enrich_derives_the_table_from_the_cms_subclass(table):
    assert set(CMS_CLAIM_TABLES) == set(CMS_CLAIM_TYPES)
    out = enrich_extraction({"claim_type": None}, doc_type="insurance_claim", subtype=table)
    assert out["claim_type"] == "health"
    assert out["cms_source_table"] == table == dojo_cms_source_table("insurance_claim", table)


def test_enrich_keeps_the_frozen_prompt_table_token_before_folding_it():
    out = enrich_extraction({"claim_type": "pde"}, doc_type="insurance_claim")
    assert (out["claim_type"], out["cms_source_table"]) == ("health", "pde")
    # The sorter subclass decides when both are known.
    out = enrich_extraction({"claim_type": "pde"}, doc_type="insurance_claim", subtype="carrier")
    assert (out["claim_type"], out["cms_source_table"]) == ("health", "carrier")


def test_non_cms_rows_carry_no_table():
    for subtype, claim in (("auto", None), ("property", "pde"), (None, "property"), (None, None)):
        out = enrich_extraction(
            {"claim_type": claim, "cms_source_table": "pde"}, doc_type="insurance_claim", subtype=subtype
        )
        assert "cms_source_table" not in out, (subtype, claim)
    # A product line other than health is not a CMS row, whatever the subclass says.
    assert cms_source_table_for("carrier", "property") is None
    out = enrich_extraction({"claim_type": "property"}, doc_type="insurance_claim", subtype="carrier")
    assert out["claim_type"] == "property" and "cms_source_table" not in out


def test_ground_truth_derives_the_table_from_the_hub_subclass():
    for table in CMS_CLAIM_TABLES:
        exp = catalog_expected_fields(
            {"expected_hf_class": "insurance_claim", "expected_subclass": table, "claim_type": "health"}
        )
        assert (exp["claim_type"], exp["cms_source_table"]) == ("health", table)
    fnol = catalog_expected_fields(
        {"expected_hf_class": "insurance_claim", "expected_subclass": "auto", "claim_type": "auto"}
    )
    assert "cms_source_table" not in fnol
    published = catalog_expected_fields({
        "expected_hf_class": "insurance_claim",
        "expected_subclass": "pde",
        "expected_fields": {"cms_source_table": "pde", "claim_type": "health"},
    })
    assert published["cms_source_table"] == "pde"
