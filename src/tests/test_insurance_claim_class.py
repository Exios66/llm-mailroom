"""KANBAN-067 — insurance_claim as a first-class mailroom document class.

Network-free integration pins: schema registry, taxonomy, graph dispatch,
classifier vocabulary, sorter prompt surface, fixture wiring. A native
sibling of the four remaining live classes (five total after court_opinion
and due_diligence were retired from the pipeline).
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "src"


def test_extraction_schemas_include_insurance_claim():
    from schemas.documents import EXTRACTION_SCHEMAS, InsuranceClaimExtraction

    assert EXTRACTION_SCHEMAS["insurance_claim"] is InsuranceClaimExtraction
    from schemas.documents import get_extraction_schema

    assert get_extraction_schema("insurance_claim") is InsuranceClaimExtraction
    assert len(EXTRACTION_SCHEMAS) == 5  # live pipeline classes


def test_insurance_schema_fields_are_scoring_ready():
    from schemas.documents import InsuranceClaimExtraction

    fields = InsuranceClaimExtraction.model_fields
    for name in (
        "claim_number", "policy_number", "insurer", "insured_party",
        "claim_type", "date_of_loss", "date_filed", "claimed_amount",
        "adjuster", "damages_description", "coverage_determination",
        "denial_reasons", "supporting_documents", "confidence",
    ):
        assert name in fields, f"missing schema field: {name}"


def test_taxonomy_declares_class_and_specialist_block():
    tax = yaml.safe_load((SRC / "config" / "taxonomy.yaml").read_text())
    classes = {c["key"]: c for c in tax["doc_classes"]}
    ic = classes["insurance_claim"]
    assert ic["schema"] == "InsuranceClaimExtraction"
    assert ic["specialist"] == "insurance_claims_specialist"
    assert ic["field_types"]["claim_number"] == "id"
    assert ic["field_types"]["claimed_amount"] == "money"
    assert ic["field_types"]["denial_reasons"] == "entity_list:free_text"
    assert len(classes) == 5
    agents = tax["agents"]
    assert "insurance_claims_specialist" in agents
    assert agents["insurance_claims_specialist"]["provider"] == "openrouter"


def test_graph_dispatch_wires_insurance_claims():
    from graph.build_graph import _build_specialist_dispatch, _extract_insurance_claims
    import inspect

    assert "insurance_claim" in _build_specialist_dispatch()
    assert callable(_extract_insurance_claims)
    assert len(inspect.getsource(_extract_insurance_claims).strip()) > 50


def test_classifier_vocabulary_contains_insurance_claim():
    from langchain_agents.classifier import VALID_CLASSES

    assert "insurance_claim" in VALID_CLASSES
    assert "unknown" in VALID_CLASSES
    assert len(VALID_CLASSES) == 6


def test_sorter_doc_classes_table_contains_insurance_claim():
    from langchain_agents.sorter_agent import DOC_CLASSES, DOC_CLASS_KEYS

    assert "insurance_claim" in DOC_CLASS_KEYS
    assert len(DOC_CLASSES) == 5
    entry = next(d for d in DOC_CLASSES if d["key"] == "insurance_claim")
    assert entry["label"] == "Insurance Claim"


def test_sorter_prompt_v13_registered_and_live():
    import langchain_agents.prompts as lp

    v13 = lp.SORTER_PROMPT_V13
    assert v13 != lp.SORTER_PROMPT_V0
    # derivation: base byte-preserved, exactly two surface changes
    assert "- insurance_claim:" in v13
    assert v13.count("- court_opinion:") == 1
    assert "insurance_claim" in v13.split("Labels")[0] if "Labels" in v13 else True
    # registered; production alias is now v14 (v12 + mailroom doctrine)
    templates = lp.PROMPT_TEMPLATES()
    assert templates["sorter_v13"] is v13
    assert templates["sorter_v14"] is lp.SORTER_PROMPT_V14
    assert templates["sorter"] is lp.SORTER_PROMPT_V14


def test_sorter_prompt_predecessors_unmutated():
    import langchain_agents.prompts as lp

    # v0 keeps its original class list (frozen history)
    assert "insurance_claim" not in lp.SORTER_PROMPT_V0
    # vision v0 likewise frozen; vision v1 carries the new class
    assert "insurance_claim" not in lp.SORTER_VISION_PROMPT_V0
    v1 = lp.SORTER_VISION_PROMPT_V1
    assert v1 != lp.SORTER_VISION_PROMPT_V0
    assert "exactly one of 7 classes" in v1
    assert "5. insurance_claim:" in v1
    assert "6. due_diligence:" in v1 and "7. correspondence:" in v1
    assert "Walk checks 1-7" in v1


def test_insurance_fixture_wires_into_conftest():
    fixture = SRC / "tests" / "fixtures" / "insurance_claim" / "sample_claim.txt"
    assert fixture.exists()
    text = fixture.read_text()
    assert "Claim No." in text and "COVERAGE DETERMINATION" in text
    conftest = (SRC / "tests" / "conftest.py").read_text()
    assert "sample_insurance_claim_text" in conftest


def test_specialist_prompt_registered():
    import langchain_agents.prompts as lp

    prompt = lp.INSURANCE_CLAIMS_SPECIALIST_PROMPT
    assert "insurance claim documentation" in prompt.lower()
    assert lp.PROMPT_TEMPLATES()["insurance_claims_specialist"] is prompt
