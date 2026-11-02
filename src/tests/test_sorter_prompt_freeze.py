"""Sorter prompt V15 is frozen; no later version may drop its class boundaries or subclass rules.

V14 put 89 indentures / rights instruments under contract and 30 mailbox fragments under
unknown on the mailroom-dataset train split, and got 664 subclasses wrong (CMS carrier as
outpatient, Certificates of Designation as rights_instrument, charter amendments as
certificate_of_formation, the correspondence marker precedence ignored). V15 (V14 +
SORTER_V15_CLASS_BOUNDARIES + SORTER_V15_SUBCLASS_RULES) is the production pin, and its
subclass catalog is the dojo sorter guide. Changing V15's bytes, pinning production back to
V14, shipping a newer version without the guard clauses, or a catalog that drops the guide
fails here.
"""

import hashlib

import pytest

from langchain_agents.prompts import (
    PROMPT_VERSIONS,
    SORTER_PROMPT_V14,
    SORTER_PROMPT_V15,
    SORTER_V15_CLASS_BOUNDARIES,
    SORTER_V15_GUARDS,
    SORTER_V15_SUBCLASS_RULES,
)

SORTER_V15_SHA256 = "78f95ca4b6b40bc35e91c7e8fcf4593a3ae3f7fb0989350a4a6ca49d83d50666"
BROKEN_SHA256 = {"905abc696c1975d5fe29c3f773738ac6836993d8f8e4cef40d1f6c30012e164f"}  # V14


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


def test_production_sorter_is_never_the_broken_variant():
    production = PROMPT_VERSIONS["sorter"]
    assert _sha(production) not in BROKEN_SHA256
    missing = [g for g in SORTER_V15_GUARDS if g not in production]
    assert not missing, f"production sorter dropped the V15 class boundaries: {missing}"


@pytest.mark.parametrize("key", sorted(k for k in PROMPT_VERSIONS if (_version(k) or 0) >= 15))
def test_every_version_after_v15_keeps_the_guards(key):
    missing = [g for g in SORTER_V15_GUARDS if g not in PROMPT_VERSIONS[key]]
    assert not missing, f"{key} regresses past the frozen V15: missing {missing}"


def test_pins_point_at_v15():
    import inspect

    from agents.sorter import SorterAgent
    from llm.prompts import _bound_prompt_versions

    assert inspect.signature(SorterAgent.__init__).parameters["prompt_version"].default == "sorter_v15"
    assert _bound_prompt_versions()["sorter"] == "sorter_v15"


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
