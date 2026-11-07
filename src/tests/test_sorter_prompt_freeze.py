"""Sorter prompt V15 is frozen; production is the condensed lineage.

V14 put 89 indentures / rights instruments under contract and 30 mailbox fragments under
unknown on the mailroom-dataset train split, and got 664 subclasses wrong (CMS carrier as
outpatient, Certificates of Designation as rights_instrument, charter amendments as
certificate_of_formation, the correspondence marker precedence ignored). V15 (V14 +
SORTER_V15_CLASS_BOUNDARIES + SORTER_V15_SUBCLASS_RULES) fixed those, and its subclass
catalog is the dojo sorter guide, but the condensed lineage beat it on the prime-sample
promotion compare (class fixed-broken 108 vs 100, subclass 135 vs 126), so production is
SORTER_PROMPT_CONDENSED with the same guide catalog; V15 stays frozen as the reference
and no numeric version after V15 may drop its guard clauses. Changing V15's bytes,
pinning production to V14, shipping a newer numeric version without the guards, a
condensed prompt that drops its essentials, or a catalog that drops the guide fails here.
"""

import hashlib

import pytest

from langchain_agents.prompts import (
    PROMPT_VERSIONS,
    SORTER_PROMPT_CONDENSED,
    SORTER_PROMPT_V14,
    SORTER_PROMPT_V15,
    SORTER_V15_CLASS_BOUNDARIES,
    SORTER_V15_GUARDS,
    SORTER_V15_SUBCLASS_RULES,
)

SORTER_V15_SHA256 = "78f95ca4b6b40bc35e91c7e8fcf4593a3ae3f7fb0989350a4a6ca49d83d50666"
BROKEN_SHA256 = {"905abc696c1975d5fe29c3f773738ac6836993d8f8e4cef40d1f6c30012e164f"}  # V14

#: The clauses the prompt tests require of the vendored v1 and every lineage version beyond it.
CONDENSED_ESSENTIALS = (
    "corporate_record, not contract",
    "indenture",
    "rights_instrument",
    "All mailbox text is correspondence",
    "unknown is only for",
    "carrier, never outpatient",
    "officer_certificate, never rights_instrument",
    "charter_amendment",
    "board_resolution",
    "certificate_of_formation is not a label",
    "FIRST form in catalog order",
    "consulting",
    "other only when no family fits",
    "conversion section",
)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _version(key):
    tail = key.removeprefix("sorter_v")
    return int(tail) if tail.isdigit() else None


def test_v15_bytes_are_frozen():
    assert _sha(SORTER_PROMPT_V15) == SORTER_V15_SHA256
    assert PROMPT_VERSIONS["sorter_v15"] is SORTER_PROMPT_V15


def test_v15_keeps_v14_as_prefix_and_adds_every_guard():
    assert SORTER_PROMPT_V15.startswith(SORTER_PROMPT_V14.rstrip())
    assert SORTER_PROMPT_V15.endswith(SORTER_V15_CLASS_BOUNDARIES + "\n\n" + SORTER_V15_SUBCLASS_RULES)
    assert all(g in SORTER_PROMPT_V15 for g in SORTER_V15_GUARDS)
    assert any(g in SORTER_V15_SUBCLASS_RULES for g in SORTER_V15_GUARDS)


def test_production_sorter_is_the_condensed_lineage():
    """Production is the condensed prompt; it keeps the lineage's essentials."""
    production = PROMPT_VERSIONS["sorter"]
    assert production is SORTER_PROMPT_CONDENSED
    assert PROMPT_VERSIONS["sorter_condensed_v5"] is SORTER_PROMPT_CONDENSED
    assert _sha(production) not in BROKEN_SHA256
    missing = [g for g in CONDENSED_ESSENTIALS if g not in production]
    assert not missing, f"production sorter dropped the condensed lineage's essentials: {missing}"


@pytest.mark.parametrize("key", sorted(k for k in PROMPT_VERSIONS if (_version(k) or 0) >= 15))
def test_every_version_after_v15_keeps_the_guards(key):
    missing = [g for g in SORTER_V15_GUARDS if g not in PROMPT_VERSIONS[key]]
    assert not missing, f"{key} regresses past the frozen V15: missing {missing}"


def test_pins_point_at_the_condensed_lineage():
    import inspect

    from agents.sorter import SorterAgent
    from llm.prompts import _bound_prompt_versions

    assert inspect.signature(SorterAgent.__init__).parameters["prompt_version"].default == "sorter_condensed_v5"
    assert _bound_prompt_versions()["sorter"] == "sorter_condensed_v5"


def test_production_files_a_bylaw_amendment_under_bylaws():
    """v10 corpus rule C: a bylaw amendment is bylaws. v1's opposite rule stays only in v1."""
    from langchain_agents.prompts import SORTER_PROMPT_CONDENSED_V1

    production = PROMPT_VERSIONS["sorter"]
    assert "a bylaw amendment is bylaws, never board_resolution" in production
    assert "bylaw amendment is board_resolution" not in production
    assert "a board-adopted bylaw amendment is board_resolution" in SORTER_PROMPT_CONDENSED_V1
    assert PROMPT_VERSIONS["sorter_condensed_v1"] is SORTER_PROMPT_CONDENSED_V1
    assert production.replace("a bylaw amendment is bylaws, never board_resolution",
                              "a board-adopted bylaw amendment is board_resolution") == SORTER_PROMPT_CONDENSED_V1


def test_condensed_is_a_fraction_of_v15():
    """The ~1k-token production prompt is a fraction of the frozen 25k-char reference."""
    assert len(SORTER_PROMPT_CONDENSED) * 4 < len(SORTER_PROMPT_V15)


def test_managed_prompt_without_guards_is_refused(monkeypatch):
    import agents.sorter as sorter_mod

    monkeypatch.setattr(sorter_mod, "get_managed_prompt", lambda name, default: (SORTER_PROMPT_V14, object()))
    agent = sorter_mod.SorterAgent.__new__(sorter_mod.SorterAgent)
    agent.prompt_version = "sorter_v15"
    agent.agent_name = "sorter"
    text = agent.system_prompt()
    assert "ALL MAILBOX TEXT IS CORRESPONDENCE" in text
    assert agent._langfuse_prompt is None


def test_subclass_catalog_is_the_dojo_guide():
    """The user-message catalog gives every key its corpus meaning, not a bare key list."""
    from langchain_agents.doc_inventories import format_sorter_subclass_catalogs

    catalog = format_sorter_subclass_catalogs()
    assert "carrier: CMS carrier table: MEDICARE SUMMARY NOTICE -- PHYSICIAN/SUPPLIER CLAIM" in catalog
    assert "officer_certificate: officer's or secretary's certificate; Certificate of Designation" in catalog
    assert "certificate_of_formation:" not in catalog
    correspondence = next(line for line in catalog.splitlines() if line.startswith("- correspondence"))
    assert "FIRST of these forms" in correspondence
    order = [line.split(":")[0].strip() for line in catalog.split("- correspondence", 1)[1].splitlines()[1:9]]
    assert order == ["attorney_demand", "letter", "press_release", "memo", "demand", "notice",
                     "meeting_request", "email"]


def test_v15_subclass_rules_name_only_catalog_keys():
    """Every key S1-S5 names is a key the guard accepts for that class."""
    from langchain_agents.doc_inventories import valid_sorter_subclasses

    named = {
        "insurance_claim": ("carrier", "outpatient", "inpatient", "pde"),
        "corporate_record": ("officer_certificate", "charter_amendment", "articles_of_incorporation",
                             "board_resolution", "bylaws", "rights_instrument"),
        "correspondence": ("attorney_demand", "letter", "press_release", "memo", "demand", "notice",
                           "meeting_request", "email"),
        "contract": ("consulting", "supply", "ip", "other"),
    }
    for kind, keys in named.items():
        valid = valid_sorter_subclasses(kind)
        for key in keys:
            assert key in valid, (kind, key)
            assert key in SORTER_V15_SUBCLASS_RULES, key


def _managed(monkeypatch, served, prompt_version="sorter_condensed_v5"):
    import agents.sorter as sorter_mod

    handle = object()
    monkeypatch.setattr(sorter_mod, "get_managed_prompt", lambda name, default: (served, handle))
    agent = sorter_mod.SorterAgent.__new__(sorter_mod.SorterAgent)
    agent.prompt_version = prompt_version
    agent.agent_name = "sorter"
    return agent.system_prompt(), agent._langfuse_prompt, handle


@pytest.mark.parametrize("pin", ["sorter_condensed_v5", "sorter"])
def test_managed_prompt_without_the_condensed_guards_is_refused_for_the_production_pin(monkeypatch, pin):
    """A stale Langfuse mailroom-sorter (V14, or any text without the condensed boundaries) never
    replaces the production condensed pin, named or by the ``sorter`` alias; the pinned text is
    served instead."""
    from langchain_agents.prompts import SORTER_CONDENSED_GUARDS

    assert all(g in SORTER_PROMPT_CONDENSED for g in SORTER_CONDENSED_GUARDS)
    for served in (SORTER_PROMPT_V14, "Classify the document."):
        text, handle, _ = _managed(monkeypatch, served, prompt_version=pin)
        assert all(g in text for g in SORTER_CONDENSED_GUARDS)
        assert handle is None


def _condensed_guards():
    from langchain_agents.prompts import SORTER_CONDENSED_GUARDS

    return SORTER_CONDENSED_GUARDS


@pytest.mark.parametrize("guard", _condensed_guards())
def test_a_managed_prompt_missing_any_one_condensed_guard_is_refused(monkeypatch, guard):
    assert SORTER_PROMPT_CONDENSED.count(guard) >= 1
    text, handle, _ = _managed(monkeypatch, SORTER_PROMPT_CONDENSED.replace(guard, ""))
    assert guard in text and handle is None


def test_the_v1_pin_keeps_the_guards_it_has(monkeypatch):
    """v1 lacks only the v5 bylaw clause; a managed prompt that drops another guard is refused."""
    from langchain_agents.prompts import SORTER_CONDENSED_BYLAW_RULE, SORTER_PROMPT_CONDENSED_V1

    guard = "officer_certificate, never rights_instrument"
    assert SORTER_CONDENSED_BYLAW_RULE not in SORTER_PROMPT_CONDENSED_V1
    text, handle, _ = _managed(monkeypatch, SORTER_PROMPT_CONDENSED_V1.replace(guard, ""),
                               prompt_version="sorter_condensed_v1")
    assert guard in text and handle is None


def test_a_managed_prompt_that_restores_the_v1_bylaw_rule_is_refused(monkeypatch):
    """Keeping the v5 clause and adding v1's opposite one contradicts the pin."""
    from langchain_agents.prompts import SORTER_CONDENSED_V1_BYLAW_RULE

    served = SORTER_PROMPT_CONDENSED + "\nNote: " + SORTER_CONDENSED_V1_BYLAW_RULE + "."
    text, handle, _ = _managed(monkeypatch, served)
    assert SORTER_CONDENSED_V1_BYLAW_RULE not in text and handle is None


@pytest.mark.parametrize("rule", [
    "a bylaw amendment is bylaws, never board_resolution",
    "officer_certificate, never rights_instrument",
])
def test_a_managed_prompt_missing_a_corporate_subclass_rule_is_refused(monkeypatch, rule):
    """Each corporate hard-case rule of the condensed pin is guarded on its own: the user-message
    catalog does not restate it, so a managed prompt that drops it must not be served."""
    from langchain_agents.prompts import SORTER_CONDENSED_GUARDS

    assert rule in SORTER_PROMPT_CONDENSED
    text, handle, _ = _managed(monkeypatch, SORTER_PROMPT_CONDENSED.replace(rule, ""))
    assert rule in text and handle is None
    assert rule in SORTER_CONDENSED_GUARDS


def test_managed_prompt_that_keeps_the_condensed_guards_is_served(monkeypatch):
    edited = SORTER_PROMPT_CONDENSED + "\nOne more managed note."
    text, handle, sent = _managed(monkeypatch, edited)
    assert "One more managed note." in text
    assert handle is sent


def test_the_rendered_catalog_agrees_with_the_production_bylaw_rule():
    """The user-message subclass catalog (the Dojo sorter guide) must not contradict v5: a bylaw
    amendment is bylaws, so board_resolution's catalog line never mentions bylaws."""
    from langchain_agents.doc_inventories import format_sorter_subclass_catalogs

    lines = format_sorter_subclass_catalogs().splitlines()
    board = [line for line in lines if line.lstrip("- ").startswith("board_resolution")]
    bylaws = [line for line in lines if line.lstrip("- ").startswith("bylaws")]
    assert board and bylaws
    assert all("bylaw" not in line.lower() for line in board), board
    assert any("amendment to the bylaws" in line for line in bylaws), bylaws


def test_the_v1_pin_serves_its_own_managed_text(monkeypatch):
    """The superseded-rule check never refuses the pin that still states the rule (v1)."""
    from langchain_agents.prompts import SORTER_PROMPT_CONDENSED_V1

    edited = SORTER_PROMPT_CONDENSED_V1 + "\nOne more managed note."
    text, handle, sent = _managed(monkeypatch, edited, prompt_version="sorter_condensed_v1")
    assert "One more managed note." in text and handle is sent
