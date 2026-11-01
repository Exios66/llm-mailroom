"""Sorter prompt V15 is frozen; no later version may drop its class boundaries.

V14 put 89 indentures / rights instruments under contract and 30 mailbox fragments under
unknown on the mailroom-dataset train split. V15 (V14 + SORTER_V15_CLASS_BOUNDARIES) is the
production pin. Changing V15's bytes, pinning production back to V14, or shipping a newer
version without the guard clauses fails here.
"""

import hashlib

import pytest

from langchain_agents.prompts import (
    PROMPT_VERSIONS,
    SORTER_PROMPT_V14,
    SORTER_PROMPT_V15,
    SORTER_V15_GUARDS,
)

SORTER_V15_SHA256 = "06a58b614fba37e0e5d26eb74a6d24efa7e197a443177d44f825f58fa507a7a7"
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
    assert all(g in SORTER_PROMPT_V15 for g in SORTER_V15_GUARDS)


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
