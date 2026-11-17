"""Versioned prompt sets: which system prompt each agent serves (``catalog.yaml``).

``served_prompt(agent, default)`` returns the managed-prompt name and the local text an
agent passes to :func:`llm.prompts.get_managed_prompt`. For an agent the active set lists,
that is ``("<agent>--<set>", <the set's file>)``, so the Langfuse copy of one set never
overrides another (``mailroom-<agent>--deploy_v1``). For any other agent it is
``(<agent>, default)``, exactly the pre-set behavior.

Set selection, first match wins: ``MAILROOM_PROMPT_SET`` (an unknown value is ignored with a
warning), taxonomy ``prompt_set.active``, the catalog's ``default_set``. Taxonomy
``prompt_set.by_agent`` pins one agent to another set, for a per-agent A/B arm.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger(__name__)

PROMPT_SET_ENV = "MAILROOM_PROMPT_SET"
_DIR = Path(__file__).resolve().parent
CATALOG_PATH = _DIR / "catalog.yaml"


class PromptSetError(RuntimeError):
    """The prompt-set catalog is malformed or names a missing file."""


@lru_cache(maxsize=1)
def catalog() -> dict:
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    sets = data.get("sets")
    if not isinstance(sets, dict) or data.get("default_set") not in sets:
        raise PromptSetError("catalog.yaml needs a sets map that holds default_set")
    for name, spec in sets.items():
        parent = spec.get("extends")
        if parent is not None and parent not in sets:
            raise PromptSetError(f"set {name!r} extends unknown set {parent!r}")
    return data


def set_names() -> tuple[str, ...]:
    return tuple(catalog()["sets"])


def _set_agents(name: str) -> dict[str, dict]:
    """Agent entries of a set, its ``extends`` chain resolved (the child wins)."""
    sets = catalog()["sets"]
    chain: list[str] = []
    while name is not None and name not in chain:
        chain.append(name)
        name = sets[name].get("extends")
    merged: dict[str, dict] = {}
    for link in reversed(chain):
        for agent, entry in (sets[link].get("agents") or {}).items():
            merged[agent] = {**entry, "set": link}
    return merged


def active_prompt_set() -> str:
    names = set_names()
    raw = os.environ.get(PROMPT_SET_ENV, "").strip()
    if raw in names:
        return raw
    if raw:
        logger.warning("prompt_set_env_ignored", value=raw, allowed=list(names))
    from pipeline.config import load_config

    block = load_config().get("prompt_set")
    if isinstance(block, dict) and block.get("active") in names:
        return block["active"]
    return catalog()["default_set"]


def _agent_set(agent: str) -> str:
    from pipeline.config import load_config

    block = load_config().get("prompt_set")
    by_agent = block.get("by_agent") if isinstance(block, dict) else None
    pinned = by_agent.get(agent) if isinstance(by_agent, dict) else None
    if pinned is not None and pinned not in set_names():
        logger.warning("prompt_set_by_agent_ignored", agent=agent, value=pinned)
    elif pinned is not None and not os.environ.get(PROMPT_SET_ENV, "").strip():
        return pinned
    return active_prompt_set()


@lru_cache(maxsize=None)
def _read(relpath: str) -> str:
    path = (_DIR / relpath).resolve()
    if not path.is_relative_to(_DIR) or not path.is_file():
        raise PromptSetError(f"prompt file {relpath!r} is missing")
    return path.read_text(encoding="utf-8")


def resolve(agent: str, set_name: str | None = None) -> tuple[str, str] | None:
    """``(prompt id, text)`` for an agent the set lists, else None (serve the code default)."""
    set_name = set_name or _agent_set(agent)
    entry = _set_agents(set_name).get(agent)
    if entry is None:
        return None
    return f"{agent}@{entry['set']}", _read(entry["file"])


def served_prompt(agent: str, default_text: str) -> tuple[str, str]:
    """``(managed prompt name, local text)`` for ``get_managed_prompt``."""
    hit = resolve(agent)
    if hit is None:
        return agent, default_text
    prompt_id, text = hit
    return prompt_id.replace("@", "--"), text


def prompt_id(agent: str) -> str:
    hit = resolve(agent)
    return hit[0] if hit else f"{agent}@code"


def prompt_ids() -> dict[str, str]:
    """Agent -> served prompt id for every agent any set lists (records per-agent A/B pins)."""
    agents = sorted({a for spec in catalog()["sets"].values() for a in (spec.get("agents") or {})})
    return {agent: prompt_id(agent) for agent in agents}


def set_templates() -> dict[str, str]:
    """Managed name -> text for every file in the catalog, for ``prompt_templates()``."""
    out: dict[str, str] = {}
    for name, spec in catalog()["sets"].items():
        for agent, entry in (spec.get("agents") or {}).items():
            out[f"{agent}--{name}"] = _read(entry["file"])
    return out
