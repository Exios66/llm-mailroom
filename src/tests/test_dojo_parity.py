"""Cross-repo parity guard: live mailroom artifacts vs the pinned dojo.

Guards (1) the live specialist prompts against the dojo ``production`` /
``production_prompts`` catalog and (2) the taxonomy ``field_types`` maps
against the dojo ``DEFAULT_FIELD_TYPES`` for the live document classes.
"""

import pytest
from llm_dojo_scoring.prompts import get_prompt
from llm_dojo_scoring.suites import DEFAULT_FIELD_TYPES

from llm.frozen_v1 import SPECIALIST_AGENTS
from llm.prompts import prompt_templates
from pipeline.config import load_config

# No shared constant exists: these are the doc_classes keys in
# src/config/taxonomy.yaml that carry ``field_types`` (all five live classes).
LIVE_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)


def test_live_specialists_equal_dojo_production():
    live = prompt_templates()
    for agent in SPECIALIST_AGENTS:
        assert (
            live[agent]
            == get_prompt(agent).text
            == get_prompt(agent, family="production_prompts").text
        )


@pytest.mark.xfail(
    strict=True,
    reason="dojo v0.20.1 re-pins taxonomy fixture (plan Task 11)",
)
def test_taxonomy_field_types_equal_dojo_defaults_for_live_classes():
    tax = {c["key"]: c["field_types"] for c in load_config()["doc_classes"]}
    for doc in LIVE_DOC_TYPES:
        assert tax[doc] == DEFAULT_FIELD_TYPES[doc], doc
