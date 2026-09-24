"""Dedicated merger_agreement_specialist — 1:1 live-class invariant.

Network-free pins: schema registry, taxonomy, graph dispatch, LangChain
registry, managed prompt, and get_specialist routing. Predicting contract
when GT is merger_agreement remains a class miss (MAUD ≠ CUAD).
"""

from pathlib import Path

import yaml

from schemas.documents import EXTRACTION_SCHEMAS, MergerAgreementExtraction

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "src"


def test_extraction_schema_is_dedicated():
    from schemas.documents import ContractExtraction, get_extraction_schema

    assert EXTRACTION_SCHEMAS["merger_agreement"] is MergerAgreementExtraction
    assert get_extraction_schema("merger_agreement") is MergerAgreementExtraction
    assert get_extraction_schema("merger_agreement") is not ContractExtraction


def test_merger_schema_omits_cuad_primaries():
    fields = MergerAgreementExtraction.model_fields
    for name in (
        "document_name",
        "parties",
        "effective_date",
        "effective_time",
        "governing_law",
        "merger_consideration",
        "maud_clauses",
        "intent",
        "subject_matter",
        "keywords",
        "confidence",
    ):
        assert name in fields, name
    assert "cuad_family" not in fields
    assert "cuad_clauses" not in fields


def test_taxonomy_points_at_dedicated_specialist():
    tax = yaml.safe_load((SRC / "config" / "taxonomy.yaml").read_text())
    classes = {c["key"]: c for c in tax["doc_classes"]}
    row = classes["merger_agreement"]
    assert row["schema"] == "MergerAgreementExtraction"
    assert row["specialist"] == "merger_agreement_specialist"
    assert row["field_types"]["merger_consideration"] == "name"
    assert row["field_types"]["maud_clauses"] == "entity_list:free_text"
    assert "cuad_family" not in row["field_types"]
    assert tax["agents"]["merger_agreement_specialist"]["provider"] == "openrouter"
    assert classes["contract"]["specialist"] == "contracts_specialist"


def test_graph_dispatch_wires_merger_specialist():
    from graph.build_graph import (
        _build_specialist_dispatch,
        _extract_merger_agreement,
        _specialist_memory_name,
    )

    dispatch = _build_specialist_dispatch()
    assert dispatch["merger_agreement"] is _extract_merger_agreement
    assert dispatch["contract"] is not _extract_merger_agreement
    assert _specialist_memory_name("merger_agreement") == "merger_agreement_specialist"


def test_langchain_registry_resolves_1_to_1():
    from langchain_agents.specialist_agents import (
        ContractsSpecialist,
        MergerAgreementSpecialist,
        SPECIALIST_REGISTRY,
        get_extraction_schema,
        get_specialist,
    )

    assert SPECIALIST_REGISTRY["merger_agreement"] is MergerAgreementSpecialist
    assert SPECIALIST_REGISTRY["contract"] is ContractsSpecialist
    assert get_specialist("merger_agreement").agent_name == "merger_agreement_specialist"
    assert get_specialist("contract").agent_name == "contracts_specialist"
    schema = get_extraction_schema("merger_agreement")
    assert schema is not None
    assert "maud_clauses" in schema["properties"]
    assert "cuad_family" not in schema["properties"]


def test_specialist_for_class_is_dedicated():
    from observability.specialist_suites import dedicated_suite, specialist_for_class

    assert specialist_for_class("merger_agreement") == "merger_agreement_specialist"
    assert specialist_for_class("contract") == "contracts_specialist"
    assert dedicated_suite("merger_agreement")["suite_key"] == "merger_agreement"


def test_prompt_registered():
    import langchain_agents.prompts as lp
    from llm.prompts import prompt_templates

    assert "merger_agreement_specialist" in lp.PROMPT_TEMPLATES()
    templates = prompt_templates()
    assert "merger_agreement_specialist" in templates
    assert "maud" in templates["merger_agreement_specialist"].lower()
