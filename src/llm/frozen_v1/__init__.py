"""Sandbox / eval-environment frozen v1 specialist prompts.

Production extraction for the LangGraph extract nodes uses these stems —
not the llm-entity-extraction contracts/sorter lineage. Files live beside
this module and are sha256-locked by ``lineage.json``.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_LINEAGE_PATH = _DIR / "lineage.json"

SPECIALIST_AGENTS: tuple[str, ...] = (
    "contracts_specialist",
    "corporate_records_specialist",
    "correspondence_specialist",
    "insurance_claims_specialist",
    "merger_agreement_specialist",
)


@lru_cache(maxsize=1)
def lineage() -> dict:
    return json.loads(_LINEAGE_PATH.read_text(encoding="utf-8"))


def specialist_path(agent_name: str) -> Path:
    if agent_name not in SPECIALIST_AGENTS:
        raise KeyError(f"no frozen-v1 stem for {agent_name!r}")
    path = _DIR / f"{agent_name}.txt"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


@lru_cache(maxsize=None)
def load_specialist_v1(agent_name: str) -> str:
    """Return the frozen v1 prompt text (file bytes decoded as UTF-8)."""
    return specialist_path(agent_name).read_text(encoding="utf-8")


def specialist_sha256(agent_name: str) -> str:
    return hashlib.sha256(specialist_path(agent_name).read_bytes()).hexdigest()


def bind_production_specialists(prompt_versions: dict[str, str]) -> None:
    """Point production specialist aliases at frozen v1 (in-place).

    Historical entity-extraction keys (``contracts_specialist_v1`` … ``v33``)
    stay on the dict for eval loops.
    """
    for agent in SPECIALIST_AGENTS:
        text = load_specialist_v1(agent)
        prompt_versions[agent] = text
    prompt_versions["contracts_specialist_frozen_v1"] = prompt_versions[
        "contracts_specialist"
    ]
