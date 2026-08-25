"""Opt-in KANBAN-090 docclass prompt arm at runtime.

Production agent prompts stay the default. Set ``MAILROOM_DOCCLASS_PROMPTS=1``
(or pass ``--docclass`` to ``run_hf_pilot.py``) to use the pure-appended
docclass variants for every classification-chain role. Langfuse names stay
namespaced (``mailroom-docclass-<key>``) so production templates are never
overwritten.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}

# production agent_name (and LangChain version aliases) → docclass registry key
AGENT_DOCCLASS_KEY: dict[str, str] = {
    "sorter": "sorter_docclass_v0",
    "sorter_v14": "sorter_docclass_v0",
    "sorter_v12": "sorter_docclass_v0",
    "contracts_specialist": "contracts_specialist_docclass_v0",
    "contracts_specialist_v32": "contracts_specialist_docclass_v0",
    "contracts_specialist_v31": "contracts_specialist_docclass_v0",
    "corporate_records_specialist": "corporate_records_specialist_docclass_v0",
    "correspondence_specialist": "correspondence_specialist_docclass_v0",
    "compliance_specialist": "compliance_specialist_docclass_v0",
    "insurance_claims_specialist": "insurance_claims_specialist_docclass_v0",
    "sorter_reviewer": "reviewer_docclass_v0",
    "arbiter": "arbiter_docclass_v0",
    "boss": "boss_docclass_v0",
    "judge": "judge_docclass_v0",
    "judge-classification": "judge_classification_docclass_v0",
    "judge-correctness": "judge_correctness_docclass_v0",
}


def docclass_prompts_enabled() -> bool:
    return os.environ.get("MAILROOM_DOCCLASS_PROMPTS", "").strip().lower() in _TRUTHY


def langchain_prompt_version(version: str) -> str:
    """Rewrite a LangChain prompt key to its docclass variant when enabled."""
    if not docclass_prompts_enabled():
        return version
    return AGENT_DOCCLASS_KEY.get(version, version)


def managed_prompt_lookup(agent_name: str, default_text: str) -> tuple[str, str]:
    """Return (Langfuse agent_name, fallback text) for BaseAgent prompts.

    When the docclass arm is on, fetch ``mailroom-docclass-<key>`` and fall
    back to the in-repo derived variant. Production names are unchanged when
    the arm is off.
    """
    if not docclass_prompts_enabled():
        return agent_name, default_text
    key = AGENT_DOCCLASS_KEY.get(agent_name)
    if not key:
        return agent_name, default_text
    from langchain_agents.prompts_docclass import DOCCLASS_PROMPT_VERSIONS

    return f"docclass-{key}", DOCCLASS_PROMPT_VERSIONS[key]
