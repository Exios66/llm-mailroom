"""Production pins: sandbox/eval-environment frozen v1 specialists + sorter_v15 (frozen; V14 + class boundaries and subclass rules).

LangGraph classify/extract nodes consume these through get_managed_prompt
(mailroom-<agent>, production label) with the same local stems as fallback.
"""

from __future__ import annotations

import inspect

from graph.build_graph import _specialist_extractor_map
from langchain_agents import prompts as LP
from llm.frozen_v1 import (
    SPECIALIST_AGENTS,
    lineage,
    load_specialist_v1,
    specialist_sha256,
)
from llm.prompts import _bound_prompt_versions, prompt_templates


def test_frozen_v1_sha256_matches_lineage_catalog():
    catalog = lineage()["specialists"]
    expected = {
        "contracts_specialist": ("d91de3967cc3b12cbd222cc5a4ae167b39578125abc83be7422498dc5024fd24", 9882),
        "corporate_records_specialist": ("fe13501f667fcd9f64f7dfbf8b0f124f81d9dd039f4109288b43f106f82b1399", 5865),
        "correspondence_specialist": ("2b0b81ff98920b9207627707443e2b023db39212fda5bdcba7bfbded39fa38b6", 6113),
        "insurance_claims_specialist": ("6c2776bcbe00091200aecbcfd21c99f197398d929143dd0c7c722811fa100034", 6277),
        "merger_agreement_specialist": ("003232584d6890054d956398135f439b86692a1123683f58914c0393b5e1a00b", 6116),
    }
    assert set(catalog) == set(SPECIALIST_AGENTS) == set(expected)
    assert {agent: (row["sha256"], row["chars"]) for agent, row in catalog.items()} == expected
    for agent in SPECIALIST_AGENTS:
        assert specialist_sha256(agent) == catalog[agent]["sha256"]
        text = load_specialist_v1(agent)
        assert text.startswith(catalog[agent]["opening"])
        assert len(text) == catalog[agent]["chars"]


def test_specialists_are_dojo_production_verbatim():
    from llm_dojo_scoring.prompts import get_prompt

    for agent in SPECIALIST_AGENTS:
        assert load_specialist_v1(agent) == get_prompt(agent, family="production").text


def test_eval_catalog_only_diverges_for_the_unpromoted_corporate_v7():
    import hashlib
    from llm_dojo_scoring.prompts import get_prompt, get_prompt_version

    digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    for agent in SPECIALIST_AGENTS:
        production = get_prompt(agent, family="production").text
        evaluation = get_prompt(agent, family="production_prompts").text
        if agent != "corporate_records_specialist":
            assert production == evaluation
        else:
            assert production != evaluation
            assert digest(production) == "fe13501f667fcd9f64f7dfbf8b0f124f81d9dd039f4109288b43f106f82b1399"
            assert digest(evaluation) == "253559fcb7ac6b36878453821a4fab0aa94228ad5add77d5aeabb06bb265134c"
            assert len(production) == 5865
            assert len(evaluation) == 5462
            assert get_prompt_version("corporate_records_specialist_v6").sha256 == digest(production)
            assert get_prompt_version("corporate_records_specialist_v7").sha256 == digest(evaluation)


def test_corrected_records_are_served():
    """Pin the corrected specialist prompt bytes by their known hash prefixes."""
    # 2026-10-05 dojo corrections: communication_type -> null (not "other");
    # filing_number null unless an official number.
    assert specialist_sha256("corporate_records_specialist").startswith("fe13501f")
    assert specialist_sha256("correspondence_specialist").startswith("2b0b81ff")
    assert specialist_sha256("contracts_specialist").startswith("d91de396")


def test_corrected_roles_record_their_eval_environment_mutation_keys():
    """Corrected bytes are eval-environment mutations (never ``_v1``/``_v2``);
    ``derived_from`` pins the uncorrected v1 digest the legacy .txt files hold."""
    import hashlib
    from pathlib import Path

    import llm.frozen_v1 as F

    expected = {
        "correspondence_specialist": "correspondence_specialist_v7",
        "corporate_records_specialist": "corporate_records_specialist_v6",
    }
    catalog = lineage()["specialists"]
    for agent in SPECIALIST_AGENTS:
        row = catalog[agent]
        if agent not in expected:
            assert row["eval_environment_key"] == f"{agent}_v1"
            assert "derived_from" not in row
            continue
        assert row["eval_environment_key"] == expected[agent]
        derived = row["derived_from"]
        assert derived["eval_environment_key"] == f"{agent}_v1"
        assert derived["sha256"] != row["sha256"]
        legacy = Path(F.__file__).parent / derived["legacy_file"]
        if legacy.is_file():
            assert hashlib.sha256(legacy.read_bytes()).hexdigest() == derived["sha256"]
    assert not any(row["eval_environment_key"].endswith("_v2") for row in catalog.values())


def test_production_templates_are_frozen_v1_specialists_and_sorter_v15():
    """Bind production templates and version labels to their frozen prompts."""
    templates = prompt_templates()
    assert templates["sorter"] == LP.SORTER_PROMPT_V15
    for agent in SPECIALIST_AGENTS:
        assert templates[agent] == load_specialist_v1(agent)
    versions = _bound_prompt_versions()
    assert versions["sorter"] == "sorter_v15"
    for agent in SPECIALIST_AGENTS:
        assert versions[agent] == "frozen_v1"


def test_entity_extraction_history_is_not_the_production_specialist_pin():
    """llm-entity-extraction v1/v33 stay for eval; they are not frozen v1."""
    frozen = load_specialist_v1("contracts_specialist")
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V1 != frozen
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V33 != frozen
    assert LP.PROMPT_VERSIONS["contracts_specialist"] == frozen
    assert LP.PROMPT_VERSIONS["contracts_specialist_v33"] is LP.CONTRACTS_SPECIALIST_PROMPT_V33
    assert LP.PROMPT_VERSIONS["sorter"] is LP.SORTER_PROMPT_V15
    assert LP.PROMPT_VERSIONS["sorter_v14"] is LP.SORTER_PROMPT_V14


def test_runtime_defaults_pin_frozen_v1_and_sorter_v15():
    from agents.contracts_specialist import ContractsSpecialist
    from agents.sorter import SorterAgent

    assert inspect.signature(SorterAgent.__init__).parameters["prompt_version"].default == (
        "sorter_v15"
    )
    assert inspect.signature(ContractsSpecialist.__init__).parameters[
        "prompt_version"
    ].default == "contracts_specialist"


def test_langgraph_extract_dispatch_covers_every_frozen_v1_specialist():
    assert set(_specialist_extractor_map()) == set(SPECIALIST_AGENTS)


def test_mailroom_wrappers_resolve_prompts_through_get_managed_prompt(monkeypatch):
    from agents.contracts_specialist import ContractsSpecialist
    from agents.sorter import SorterAgent

    seen: dict[str, str] = {}

    def fake(agent_name, default_text, variables=None, label="production"):
        seen[agent_name] = default_text
        return default_text, None

    monkeypatch.setattr("agents.sorter.get_managed_prompt", fake)
    monkeypatch.setattr("agents.contracts_specialist.get_managed_prompt", fake)

    sorter = SorterAgent()
    sorter_text = sorter.system_prompt()
    assert seen["sorter"] == LP.SORTER_PROMPT_V15
    assert "{{doc_type_descriptions}}" not in sorter_text
    assert "contract" in sorter_text.lower()

    specialist = ContractsSpecialist()
    contracts_text = specialist.system_prompt()
    assert seen["contracts_specialist"] == load_specialist_v1("contracts_specialist")
    assert contracts_text == load_specialist_v1("contracts_specialist")
