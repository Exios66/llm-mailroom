"""Production pins: sandbox/eval-environment frozen v1 specialists + sorter_v14.

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
    assert set(catalog) == set(SPECIALIST_AGENTS)
    for agent in SPECIALIST_AGENTS:
        assert specialist_sha256(agent) == catalog[agent]["sha256"]
        text = load_specialist_v1(agent)
        assert text.startswith(catalog[agent]["opening"])
        assert len(text) == catalog[agent]["chars"]


def test_specialists_are_dojo_production_prompts_verbatim():
    from llm_dojo_scoring.prompts import get_prompt

    for agent in SPECIALIST_AGENTS:
        assert load_specialist_v1(agent) == get_prompt(agent, family="production_prompts").text


def test_corrected_records_are_served():
    # 2026-10-05 dojo corrections: communication_type -> null (not "other");
    # filing_number null unless an official number.
    assert specialist_sha256("corporate_records_specialist").startswith("fe13501f")
    assert specialist_sha256("correspondence_specialist").startswith("2b0b81ff")
    assert specialist_sha256("contracts_specialist").startswith("d91de396")


def test_production_templates_are_frozen_v1_specialists_and_sorter_v14():
    templates = prompt_templates()
    assert templates["sorter"] == LP.SORTER_PROMPT_V14
    for agent in SPECIALIST_AGENTS:
        assert templates[agent] == load_specialist_v1(agent)
    versions = _bound_prompt_versions()
    assert versions["sorter"] == "sorter_v14"
    for agent in SPECIALIST_AGENTS:
        assert versions[agent] == "frozen_v1"


def test_entity_extraction_history_is_not_the_production_specialist_pin():
    """llm-entity-extraction v1/v33 stay for eval; they are not frozen v1."""
    frozen = load_specialist_v1("contracts_specialist")
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V1 != frozen
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V33 != frozen
    assert LP.PROMPT_VERSIONS["contracts_specialist"] == frozen
    assert LP.PROMPT_VERSIONS["contracts_specialist_v33"] is LP.CONTRACTS_SPECIALIST_PROMPT_V33
    assert LP.PROMPT_VERSIONS["sorter"] is LP.SORTER_PROMPT_V14
    assert LP.PROMPT_VERSIONS["sorter_v14"] is LP.SORTER_PROMPT_V14


def test_runtime_defaults_pin_frozen_v1_and_sorter_v14():
    from agents.contracts_specialist import ContractsSpecialist
    from agents.sorter import SorterAgent

    assert inspect.signature(SorterAgent.__init__).parameters["prompt_version"].default == (
        "sorter_v14"
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
    assert seen["sorter"] == LP.SORTER_PROMPT_V14
    assert "{{doc_type_descriptions}}" not in sorter_text
    assert "contract" in sorter_text.lower()

    specialist = ContractsSpecialist()
    contracts_text = specialist.system_prompt()
    assert seen["contracts_specialist"] == load_specialist_v1("contracts_specialist")
    assert contracts_text == load_specialist_v1("contracts_specialist")
