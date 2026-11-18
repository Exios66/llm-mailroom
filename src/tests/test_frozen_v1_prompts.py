"""Production pins: sandbox/eval-environment frozen v1 specialists +
the condensed sorter lineage (sorter_v15 stays frozen as the reference).

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
        "correspondence_specialist": ("e3bc920d5a99c951ace99e4a726a8cbd85a135ebf0fb81408ec8654c774b4000", 6316),
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


_PINNED_FAMILIES = ("production", "production_prompts")


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_specialists_are_the_lineage_pinned_bytes_from_the_dojo_catalog():
    """The contract is content-pinned: lineage.json's sha256 and chars, resolved from the Dojo.

    The live specialist text is NOT "whatever the Dojo ``production`` family serves"
    (Dojo 0.30.0 moved contracts and merger to v2 there); it is the catalog record whose
    bytes hash to the lineage sha256.
    """
    from llm_dojo_scoring.prompts import list_prompts

    for agent, row in lineage()["specialists"].items():
        text = load_specialist_v1(agent)
        assert _digest(text) == row["sha256"], agent
        assert len(text) == row["chars"], agent
        holders = [
            (rec.family, rec.version)
            for family in _PINNED_FAMILIES
            for rec in list_prompts(agent=agent, family=family)
            if rec.text == text
        ]
        assert holders, f"{agent}: pinned bytes are not in the Dojo catalog"


def test_pinned_bytes_live_in_known_dojo_families():
    """Where each pinned stem sits in the Dojo catalog, so a catalog move is noticed.

    contracts and merger: v1 left ``production`` (now v2) and stay in ``production_prompts``.
    corporate: the pinned v6 stays in ``production``; ``production_prompts`` holds the
    unpromoted v7 eval candidate. correspondence: the pinned v8 is only in
    ``production_prompts``; ``production`` still serves v7 (2b0b81ff…). insurance: identical in both.
    """
    from llm_dojo_scoring.prompts import get_prompt, get_prompt_version

    pinned = {agent: row["sha256"] for agent, row in lineage()["specialists"].items()}
    expected_families = {
        "contracts_specialist": {"production_prompts"},
        "merger_agreement_specialist": {"production_prompts"},
        "corporate_records_specialist": {"production"},
        "correspondence_specialist": {"production_prompts"},
        "insurance_claims_specialist": {"production", "production_prompts"},
    }
    assert set(expected_families) == set(SPECIALIST_AGENTS)
    for agent in SPECIALIST_AGENTS:
        holding = {
            family
            for family in _PINNED_FAMILIES
            if _digest(get_prompt(agent, family=family).text) == pinned[agent]
        }
        assert holding == expected_families[agent], agent
    evaluation = get_prompt("corporate_records_specialist", family="production_prompts").text
    production = get_prompt("corporate_records_specialist", family="production").text
    assert _digest(evaluation) == "253559fcb7ac6b36878453821a4fab0aa94228ad5add77d5aeabb06bb265134c"
    assert len(evaluation) == 5462
    assert get_prompt_version("corporate_records_specialist_v6").sha256 == _digest(production)
    assert get_prompt_version("corporate_records_specialist_v7").sha256 == _digest(evaluation)


def test_contracts_and_merger_stay_on_v1_while_dojo_production_is_v2():
    """Dojo 0.30.0 production = contracts/merger v2; the mailroom keeps the v1 pin."""
    from llm_dojo_scoring.prompts import get_prompt

    pins = {
        "contracts_specialist": ("d91de3967cc3b12cbd222cc5a4ae167b39578125abc83be7422498dc5024fd24", 9882),
        "merger_agreement_specialist": ("003232584d6890054d956398135f439b86692a1123683f58914c0393b5e1a00b", 6116),
    }
    for agent, (sha, chars) in pins.items():
        assert specialist_sha256(agent) == sha
        assert len(load_specialist_v1(agent)) == chars
        assert load_specialist_v1(agent) == get_prompt(agent, family="production_prompts").text
        assert get_prompt(agent, family="production").version == "v2"
        assert specialist_sha256(agent) != _digest(get_prompt(agent, family="production").text)


def test_corporate_resolves_to_the_pinned_v6_not_the_production_prompts_v7():
    from llm_dojo_scoring.prompts import get_prompt

    pinned = "fe13501f667fcd9f64f7dfbf8b0f124f81d9dd039f4109288b43f106f82b1399"
    candidate = get_prompt("corporate_records_specialist", family="production_prompts").text
    assert specialist_sha256("corporate_records_specialist") == pinned
    assert load_specialist_v1("corporate_records_specialist") != candidate
    assert _digest(candidate).startswith("253559fc")
    assert len(load_specialist_v1("corporate_records_specialist")) == 5865


class _FakeRecord:
    def __init__(self, agent, family, text, version="v1", kind="llm"):
        self.agent, self.family, self.text, self.version, self.kind = agent, family, text, version, kind


def test_loader_follows_content_not_family_precedence(monkeypatch):
    """The pinned bytes win even when ``production`` is searched first and holds other bytes."""
    import llm_dojo_scoring.prompts as dojo_prompts

    pinned_text = load_specialist_v1("contracts_specialist")
    records = [
        _FakeRecord("contracts_specialist", "production", "newer contracts prompt", version="v2"),
        _FakeRecord("contracts_specialist", "production_prompts", pinned_text),
    ]

    def fake_list(*, agent=None, family=None, kind=None):
        return [r for r in records if r.agent == agent and (family is None or r.family == family)]

    monkeypatch.setattr(dojo_prompts, "list_prompts", fake_list)
    load_specialist_v1.cache_clear()
    try:
        assert load_specialist_v1("contracts_specialist") == pinned_text
    finally:
        load_specialist_v1.cache_clear()


def _break_lineage(monkeypatch, agent, bogus="0" * 64):
    import copy

    import llm.frozen_v1 as F

    broken = copy.deepcopy(lineage())
    broken["specialists"][agent]["sha256"] = bogus
    monkeypatch.setattr(F, "lineage", lambda: broken)
    load_specialist_v1.cache_clear()
    return F


def test_unmatched_lineage_sha_fails_fast_and_names_the_pin(monkeypatch):
    import pytest

    F = _break_lineage(monkeypatch, "contracts_specialist")
    try:
        with pytest.raises(F.SpecialistPinError) as err:
            load_specialist_v1("contracts_specialist")
        message = str(err.value)
        assert "contracts_specialist" in message
        assert "0" * 64 in message
        assert "production" in message and "production_prompts" in message
        # Never a silent fallback to other bytes: the other agents still resolve.
        assert specialist_sha256("insurance_claims_specialist").startswith("6c2776bc")
    finally:
        load_specialist_v1.cache_clear()


def test_a_missing_lineage_entry_is_a_pin_error_and_is_aggregated(monkeypatch):
    import copy

    import pytest

    from llm import frozen_v1 as F

    broken = copy.deepcopy(F.lineage())
    del broken["specialists"]["correspondence_specialist"]["chars"]
    monkeypatch.setattr(F, "lineage", lambda: broken)
    load_specialist_v1.cache_clear()
    try:
        with pytest.raises(F.SpecialistPinError, match="correspondence_specialist: lineage.json"):
            load_specialist_v1("correspondence_specialist")
        with pytest.raises(F.SpecialistPinError, match="correspondence_specialist"):
            F.verify_specialist_pins()
    finally:
        load_specialist_v1.cache_clear()


def test_an_unreadable_lineage_or_catalog_is_a_pin_error(monkeypatch):
    import pytest

    import llm_dojo_scoring.prompts as dojo_prompts
    from llm import frozen_v1 as F

    def unreadable():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(F, "lineage", unreadable)
    load_specialist_v1.cache_clear()
    try:
        with pytest.raises(F.SpecialistPinError, match="lineage.json has no usable"):
            F.verify_specialist_pins()
        monkeypatch.undo()

        def broken_catalog(**_kwargs):
            raise ValueError("unknown prompt family")

        monkeypatch.setattr(dojo_prompts, "list_prompts", broken_catalog)
        load_specialist_v1.cache_clear()
        with pytest.raises(F.SpecialistPinError, match="prompt catalog cannot be read"):
            F.verify_specialist_pins()
    finally:
        monkeypatch.undo()
        load_specialist_v1.cache_clear()


def test_verify_specialist_pins_reports_every_unresolvable_agent(monkeypatch):
    import pytest

    F = _break_lineage(monkeypatch, "merger_agreement_specialist", bogus="1" * 64)
    try:
        with pytest.raises(F.SpecialistPinError, match="merger_agreement_specialist"):
            F.verify_specialist_pins()
        monkeypatch.undo()
        load_specialist_v1.cache_clear()
        F.verify_specialist_pins()  # the real lineage resolves
    finally:
        load_specialist_v1.cache_clear()


def test_build_graph_refuses_to_start_when_a_specialist_pin_does_not_resolve(monkeypatch):
    import pytest
    from langgraph.checkpoint.memory import MemorySaver

    from graph.build_graph import build_graph

    F = _break_lineage(monkeypatch, "corporate_records_specialist")
    try:
        with pytest.raises(F.SpecialistPinError, match="corporate_records_specialist"):
            build_graph(checkpointer=MemorySaver())
    finally:
        monkeypatch.undo()
        load_specialist_v1.cache_clear()


def test_corrected_records_are_served():
    """Pin the corrected specialist prompt bytes by their known hash prefixes."""
    # 2026-10-05 dojo corrections: communication_type -> null (not "other");
    # filing_number null unless an official number. correspondence is the v8 correction
    # (content-not-carrier communication_type, #269), served from production_prompts.
    assert specialist_sha256("corporate_records_specialist").startswith("fe13501f")
    assert specialist_sha256("correspondence_specialist").startswith("e3bc920d")
    assert specialist_sha256("contracts_specialist").startswith("d91de396")


def test_correspondence_types_the_content_not_the_carrier():
    """llm-mailroom#269: correspondence_specialist_v8 replaces the Enron-inbox-to-email rule.

    The old v7 line filed every inbox item as ``email``; v8 types the document's form and
    keeps ``email`` for ordinary email correspondence only.
    """
    text = load_specialist_v1("correspondence_specialist")
    assert "Enron-style inbox → email" not in text
    assert "Type the content, not the carrier" in text


def test_corrected_roles_record_their_eval_environment_mutation_keys():
    """Corrected bytes are eval-environment mutations (never ``_v1``/``_v2``);
    ``derived_from`` pins the uncorrected v1 digest the legacy .txt files hold."""
    import hashlib
    from pathlib import Path

    import llm.frozen_v1 as F

    expected = {
        "correspondence_specialist": "correspondence_specialist_v8",
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


def test_production_templates_are_frozen_v1_specialists_and_condensed_sorter(monkeypatch):
    """Bind production templates and version labels to their frozen prompts (frozen_v1 set)."""
    monkeypatch.setenv("MAILROOM_PROMPT_SET", "frozen_v1")
    templates = prompt_templates()
    assert templates["sorter"] == LP.SORTER_PROMPT_CONDENSED
    for agent in SPECIALIST_AGENTS:
        assert templates[agent] == load_specialist_v1(agent)
    versions = _bound_prompt_versions()
    assert versions["sorter"] == "sorter_condensed_v5"
    for agent in SPECIALIST_AGENTS:
        assert versions[agent] == "frozen_v1"


def test_entity_extraction_history_is_not_the_production_specialist_pin():
    """llm-entity-extraction v1/v33 stay for eval; they are not frozen v1."""
    frozen = load_specialist_v1("contracts_specialist")
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V1 != frozen
    assert LP.CONTRACTS_SPECIALIST_PROMPT_V33 != frozen
    assert LP.PROMPT_VERSIONS["contracts_specialist"] == frozen
    assert LP.PROMPT_VERSIONS["contracts_specialist_v33"] is LP.CONTRACTS_SPECIALIST_PROMPT_V33
    assert LP.PROMPT_VERSIONS["sorter"] is LP.SORTER_PROMPT_CONDENSED
    assert LP.PROMPT_VERSIONS["sorter_v14"] is LP.SORTER_PROMPT_V14


def test_runtime_defaults_pin_frozen_v1_and_condensed_sorter():
    from agents.contracts_specialist import ContractsSpecialist
    from agents.sorter import SorterAgent

    assert inspect.signature(SorterAgent.__init__).parameters["prompt_version"].default == (
        "sorter_condensed_v5"
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
    # The frozen-v1 wiring is the frozen_v1 prompt set; deploy_v1 is covered in test_prompt_sets.py.
    monkeypatch.setenv("MAILROOM_PROMPT_SET", "frozen_v1")

    def fake(agent_name, default_text, variables=None, label="production"):
        seen[agent_name] = default_text
        return default_text, None

    monkeypatch.setattr("agents.sorter.get_managed_prompt", fake)
    monkeypatch.setattr("agents.contracts_specialist.get_managed_prompt", fake)

    sorter = SorterAgent()
    sorter_text = sorter.system_prompt()
    assert seen["sorter"] == LP.SORTER_PROMPT_CONDENSED
    assert "{{doc_type_descriptions}}" not in sorter_text
    assert "contract" in sorter_text.lower()

    specialist = ContractsSpecialist()
    contracts_text = specialist.system_prompt()
    assert seen["contracts_specialist"] == load_specialist_v1("contracts_specialist")
    assert contracts_text == load_specialist_v1("contracts_specialist")
