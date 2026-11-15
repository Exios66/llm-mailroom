"""Frozen v1 specialist prompts for the LangGraph extract nodes.

Production extraction uses these stems, not the llm-entity-extraction
contracts/sorter lineage. The prompt version is pinned **by content**:
``lineage.json`` records the sha256 and length of each prompt, and the text is
the record in the ``llm-dojo-scoring`` prompt catalog (pinned in pyproject.toml)
whose bytes hash to that sha256. A Dojo bump never changes the served prompt;
if the pinned Dojo no longer carries the pinned bytes, loading fails with
:class:`SpecialistPinError` rather than serving other bytes. The legacy ``*.txt``
files in this directory are superseded and no longer packaged.
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


#: Dojo catalog families searched for the pinned bytes, in order. Both are searched
#: because the family that holds a given stem moves between Dojo releases (0.30.0:
#: contracts and merger v1 left ``production`` but remain in ``production_prompts``;
#: corporate (catalog ``production/v1``, eval-environment v6) stays in ``production`` while ``production_prompts`` holds an
#: unpromoted v7 candidate). The sha256 decides, never the family.
CATALOG_FAMILIES: tuple[str, ...] = ("production", "production_prompts")


class SpecialistPinError(RuntimeError):
    """The pinned Dojo catalog holds no record matching a lineage-pinned specialist prompt."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=None)
def load_specialist_v1(agent_name: str) -> str:
    """Return the frozen v1 prompt text: the Dojo catalog record matching the lineage sha256.

    Raises :class:`SpecialistPinError` (naming the agent, the expected sha256 and the
    families searched) when no record matches; there is no fallback to other bytes.
    """
    if agent_name not in SPECIALIST_AGENTS:
        raise KeyError(f"no frozen-v1 stem for {agent_name!r}")
    try:
        pin = lineage()["specialists"][agent_name]
        expected, chars = pin["sha256"], pin["chars"]
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise SpecialistPinError(
            f"{agent_name}: lineage.json has no usable sha256/chars pin ({exc!r})"
        ) from exc
    try:
        from llm_dojo_scoring.prompts import list_prompts

        candidates = [
            (family, record)
            for family in CATALOG_FAMILIES
            for record in list_prompts(agent=agent_name, family=family)
        ]
    except Exception as exc:
        raise SpecialistPinError(
            f"{agent_name}: the pinned llm-dojo-scoring prompt catalog cannot be read ({exc!r})"
        ) from exc
    for family, record in candidates:
        if record.kind != "llm" or not isinstance(record.text, str) or _sha256(record.text) != expected:
            continue
        if len(record.text) != chars:
            raise SpecialistPinError(
                f"{agent_name}: catalog record {family}/{record.version} matches the pinned "
                f"sha256 {expected} but has {len(record.text)} chars, lineage.json says {chars}"
            )
        return record.text
    raise SpecialistPinError(
        f"{agent_name}: no prompt in the pinned llm-dojo-scoring catalog hashes to the "
        f"lineage.json sha256 {expected} (families searched: {', '.join(CATALOG_FAMILIES)}). "
        "The pinned Dojo no longer carries the frozen prompt; re-pin the Dojo to a revision "
        "that does, or re-freeze lineage.json deliberately."
    )


def specialist_sha256(agent_name: str) -> str:
    return _sha256(load_specialist_v1(agent_name))


def verify_specialist_pins() -> None:
    """Resolve every specialist pin now; raise one :class:`SpecialistPinError` listing all failures.

    Called at API lifespan startup (``api.main.lifespan``, before any watcher starts),
    at the top of ``pipeline.watcher.Watcher.start`` (so the embedded watcher and
    ``python -m pipeline.watcher`` both stop before taking the lock or scanning the
    inbox) and in ``graph.build_graph.build_graph``. A Dojo pin that cannot serve the
    frozen prompts therefore stops the process at startup instead of failing every
    document on its first extraction. A process that only imports the graph (a script
    or notebook) is covered by the ``build_graph`` call alone.
    """
    failures: list[str] = []
    for agent in SPECIALIST_AGENTS:
        try:
            load_specialist_v1(agent)
        except SpecialistPinError as exc:
            failures.append(str(exc))
    if failures:
        raise SpecialistPinError("; ".join(failures))


def bind_production_specialists(prompt_versions: dict[str, str]) -> None:
    """Point production specialist aliases at frozen v1 (in-place).

    Raises :class:`SpecialistPinError` when a pinned prompt cannot be resolved.

    Historical entity-extraction keys (``contracts_specialist_v1`` … ``v33``)
    stay on the dict for eval loops.
    """
    for agent in SPECIALIST_AGENTS:
        text = load_specialist_v1(agent)
        prompt_versions[agent] = text
    prompt_versions["contracts_specialist_frozen_v1"] = prompt_versions[
        "contracts_specialist"
    ]
