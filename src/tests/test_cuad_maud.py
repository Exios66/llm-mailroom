"""CUAD / MAUD inventory: Hub GT flatten, enrich, handoff, schema."""

from langchain_agents.cuad_maud import (
    CUAD_CLAUSE_CATEGORIES,
    HUB_CUAD_FAMILY_LABELS,
    MAUD_CLAUSE_QUESTIONS,
    as_clause_lines,
    clause_handoff,
    enrich_contract_extraction,
    flatten_cuad_clause_labels,
    flatten_maud_clause_labels,
    normalize_consideration,
    skip_conflict_field,
)
from langchain_agents.sorter_agent import CONTRACT_SUBTYPE_KEYS, normalize_subtype
from langchain_agents.specialist_agents import CONTRACTS_SCHEMA
from schemas.documents import ContractExtraction


def test_cuad_has_all_41_categories():
    assert len(CUAD_CLAUSE_CATEGORIES) == 41
    assert "Anti-Assignment" in CUAD_CLAUSE_CATEGORIES
    assert "Affiliate License-Licensee" in CUAD_CLAUSE_CATEGORIES
    assert "Third Party Beneficiary" in CUAD_CLAUSE_CATEGORIES


def test_all_25_cuad_families_normalize_from_hub_labels():
    for label in HUB_CUAD_FAMILY_LABELS:
        got = normalize_subtype(label)
        assert got in CONTRACT_SUBTYPE_KEYS, (label, got)
    assert normalize_subtype("Co_Branding") == "co_branding"
    assert normalize_subtype("License_Agreements") == "license"
    assert normalize_subtype("Joint Venture _ Filing") == "joint_venture"
    assert normalize_subtype("Non_Compete_Non_Solicit") == "non_compete_no_solicit"
    assert len(CONTRACT_SUBTYPE_KEYS) == 25
    assert len(set(normalize_subtype(l) for l in HUB_CUAD_FAMILY_LABELS)) == 25


def test_flatten_cuad_clause_labels_keeps_present_spans():
    raw = {
        "Anti-Assignment": [{"start": 10, "text": "may not assign"}],
        "Audit Rights": [],
        "Governing Law": [{"start": 1, "text": "Delaware law"}],
    }
    lines = flatten_cuad_clause_labels(raw)
    assert lines == [
        "Anti-Assignment: may not assign",
        "Governing Law: Delaware law",
    ]


def test_flatten_maud_clause_labels_keeps_answers():
    raw = {
        "Absence of Litigation Closing Condition": {
            "answer": "Pending",
            "category": "Conditions to Closing",
        },
        "Something unanswered": {"answer": None},
    }
    lines = flatten_maud_clause_labels(raw)
    assert lines == ["Absence of Litigation Closing Condition: Pending"]


def test_as_clause_lines_coerces_object_items():
    lines = as_clause_lines([
        {"category": "License Grant", "text": "hereby grants"},
        "Cap On Liability: $1,000,000",
    ])
    assert "License Grant: hereby grants" in lines
    assert "Cap On Liability: $1,000,000" in lines


def test_enrich_fills_family_and_consideration():
    contract = enrich_contract_extraction(
        {"parties": ["A"], "cuad_clauses": [{"category": "Parties", "text": "A"}]},
        doc_type="contract",
        contract_subtype="distributor",
    )
    assert contract["cuad_family"] == "distributor"
    assert contract["cuad_clauses"] == ["Parties: A"]
    merger = enrich_contract_extraction(
        {"contract_value": "all cash", "key_obligations": ["Target survives"]},
        doc_type="merger_agreement",
    )
    assert merger["merger_consideration"] == "all_cash"
    assert merger["maud_clauses"] == []


def test_clause_handoff_lists_inventory():
    text = clause_handoff("contract", "license")
    assert "Anti-Assignment" in text
    assert "license" in text
    merger = clause_handoff("merger_agreement", None)
    assert "merger_consideration" in merger
    assert "all_cash" in merger
    assert "MAE Definition" in merger
    assert "Type of Consideration" in merger
    for question in MAUD_CLAUSE_QUESTIONS:
        assert question in merger, question


def test_maud_has_all_22_questions():
    assert len(MAUD_CLAUSE_QUESTIONS) == 22
    assert "Type of Consideration" in MAUD_CLAUSE_QUESTIONS
    assert "MAE Definition" in MAUD_CLAUSE_QUESTIONS
    assert normalize_consideration("Mixed Cash/Stock: Election") == "mixed_cash_stock_election"
    assert normalize_consideration("All Cash") == "all_cash"
    merger = enrich_contract_extraction(
        {"maud_clauses": ["Type of Consideration: Mixed Cash/Stock"]},
        doc_type="merger_agreement",
    )
    assert merger["merger_consideration"] == "mixed_cash_stock"


def test_contracts_schema_and_pydantic_include_inventory():
    props = CONTRACTS_SCHEMA["properties"]
    for key in ("cuad_family", "merger_consideration", "cuad_clauses", "maud_clauses"):
        assert key in props
    parsed = ContractExtraction.model_validate({
        "parties": ["Acme"],
        "cuad_family": "license",
        "cuad_clauses": ["License Grant: exclusive license"],
    })
    assert parsed.cuad_family == "license"
    assert parsed.cuad_clauses[0].startswith("License Grant")


def test_skip_conflict_on_inventory_fields():
    assert skip_conflict_field("cuad_clauses") is True
    assert skip_conflict_field("parties") is False


def test_parse_hf_row_joins_clause_labels():
    from scripts.run_hf_pilot import parse_hf_row

    row = parse_hf_row(
        {
            "filename": "brand.pdf",
            "doc_text": "CO-BRANDING AGREEMENT " + "x" * 300,
            "expected": "contract",
            "expected_subclass": "Co_Branding",
            "cuad_clause_labels": '{"Anti-Assignment": [{"start": 0, "text": "not assignable"}]}',
        }
    )
    assert row["expected_hf_class"] == "contract"
    assert row["cuad_clauses"] == ["Anti-Assignment: not assignable"]
    assert row["maud_clauses"] == []
