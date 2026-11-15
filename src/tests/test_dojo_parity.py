"""Cross-repo parity guard: live mailroom artifacts vs the pinned dojo.

Guards (1) the live specialist prompts against the lineage-pinned bytes in the dojo
``production`` / ``production_prompts`` catalog and (2) the taxonomy ``field_types`` maps
against the dojo ``DEFAULT_FIELD_TYPES`` for the live document classes.
"""

import hashlib

from llm_dojo_scoring import get_doc_scorer
from llm_dojo_scoring.scoring.doc_scorers import DOC_SCORERS
from llm_dojo_scoring.prompts import get_prompt, list_prompts
from llm_dojo_scoring.suites import DEFAULT_FIELD_TYPES

from llm.frozen_v1 import SPECIALIST_AGENTS, lineage
from llm.prompts import prompt_templates
from pipeline.config import EXTRACT_CLASS_ALIASES, load_config

# No shared constant exists: these are the doc_classes keys in
# src/config/taxonomy.yaml that carry ``field_types`` (all five live classes).
LIVE_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)


def test_live_specialists_equal_the_lineage_pinned_dojo_bytes():
    """Live prompts are the lineage-pinned bytes, resolved by content from the pinned Dojo.

    Contract: ``prompt_templates()[agent]`` hashes to ``lineage.json``'s sha256 and
    chars, and those bytes exist in the pinned Dojo catalog. It is deliberately NOT
    "equal to the Dojo ``production`` family": Dojo 0.30.0 moved contracts and merger to
    v2 there, and adopting a newer Dojo prompt is a separate, explicit re-freeze of
    ``lineage.json``.
    """
    live = prompt_templates()
    pinned = lineage()["specialists"]
    for agent in SPECIALIST_AGENTS:
        assert hashlib.sha256(live[agent].encode("utf-8")).hexdigest() == pinned[agent]["sha256"]
        assert len(live[agent]) == pinned[agent]["chars"]
        in_catalog = [
            rec
            for family in ("production", "production_prompts")
            for rec in list_prompts(agent=agent, family=family)
            if rec.text == live[agent]
        ]
        assert in_catalog, f"{agent}: pinned bytes are not in the pinned Dojo catalog"
    # The unpromoted corporate v7 eval candidate is never served.
    candidate = get_prompt("corporate_records_specialist", family="production_prompts").text
    assert live["corporate_records_specialist"] != candidate


def test_taxonomy_field_types_equal_dojo_defaults_for_live_classes():
    """Require live taxonomy field types to match the pinned dojo defaults."""
    # Strict parity (plan Task 12): the dojo re-pinned DEFAULT_FIELD_TYPES to
    # the mailroom taxonomy (``intent: label`` for the three controlled
    # classes), so any drift on either side fails here.
    tax = {c["key"]: c["field_types"] for c in load_config()["doc_classes"]}
    for doc in LIVE_DOC_TYPES:
        assert tax[doc] == DEFAULT_FIELD_TYPES[doc], doc


def test_live_classes_all_have_dojo_field_maps():
    """The taxonomy's field-map classes are the five live ones, each with a dojo field map.

    The dojo also keeps maps for retired classes (court opinion, due diligence,
    compliance filing); those are allowed, the live five are required.
    """
    tax = {c["key"] for c in load_config()["doc_classes"] if c.get("field_types")}
    assert tax == set(LIVE_DOC_TYPES)
    assert set(LIVE_DOC_TYPES) <= set(DEFAULT_FIELD_TYPES)


def test_every_live_class_has_its_own_dojo_scorer():
    """dojo 0.27.0 routes each live class to its own module, never ``generic``."""
    for doc in LIVE_DOC_TYPES:
        assert get_doc_scorer(doc) is DOC_SCORERS[doc], doc
        assert DOC_SCORERS[doc].doc_type == doc


def test_no_extract_class_aliases():
    """Extraction never aliases one class onto another's field map (plan M1)."""
    assert EXTRACT_CLASS_ALIASES == {}
