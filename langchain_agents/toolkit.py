"""Per-agent TOOL registry for the vendored LangChain agents.

Each designated agent gets access to a set of tools it can consult while
working — read-only lookups that ground the agent in repo state (taxonomy,
schema, subtypes, memory) without letting it mutate anything. Tools follow a
tiny standard interface:

    name        — stable identifier
    description — what the tool does (for logs/prompt rendering)
    run(**kw)   — synchronous callable returning a JSON-safe value

The tool registry is per-agent (``TOOLKITS[agent_name]``) so the sorter gets
classification tools and specialists get extraction tools; unknown agents get
an empty toolkit. Tools never raise: every ``run`` is wrapped so a failing
tool degrades to a null result instead of breaking the agent call.
"""

from __future__ import annotations

import structlog
from typing import Callable

from langchain_agents.memory import recent_context, stats as memory_stats

logger = structlog.get_logger(__name__)


class AgentTool:
    def __init__(self, name: str, description: str, run: Callable[..., object]):
        self.name = name
        self.description = description
        self._run = run

    def run(self, **kw) -> object:
        try:
            return self._run(**kw)
        except Exception:
            logger.debug("tool_failed", tool=self.name, exc_info=True)
            return None


def _tool_taxonomy() -> dict:
    try:
        from pipeline.config import load_config

        cfg = load_config()
        return {
            "doc_classes": [
                {k: c.get(k) for k in ("key", "label", "description")}
                for c in cfg.get("doc_classes", [])
            ],
            "confidence": cfg.get("confidence", {}),
        }
    except Exception:
        return {}


def _tool_subtypes() -> list[dict]:
    try:
        from langchain_agents.sorter_agent import CONTRACT_SUBTYPES

        return [
            {k: s.get(k) for k in ("key", "label", "description")}
            for s in CONTRACT_SUBTYPES
        ]
    except Exception:
        return []


def _tool_schema(doc_type: str) -> dict | None:
    try:
        from schemas.documents import get_extraction_schema

        model = get_extraction_schema(doc_type)
        if model is None:
            return None
        return {
            "fields": list(model.model_fields.keys()),
        }
    except Exception:
        return None


def _tool_field_types(doc_type: str) -> dict:
    try:
        from observability.field_scoring import get_field_types

        return get_field_types(doc_type) or {}
    except Exception:
        return {}


def _tool_memory(agent_name: str):
    def _run(doc_type: str = "", k: int = 3) -> dict:
        return {
            "context": recent_context(agent_name, doc_type or "", k=k),
            "stats": memory_stats(agent_name),
        }

    return _run


_TOOLKITS: dict[str, list[AgentTool]] = {}


def _build_toolkit(agent_name: str) -> list[AgentTool]:
    tools = [
        AgentTool(
            "taxonomy",
            "Current pipeline taxonomy: doc classes, labels, descriptions, and confidence thresholds.",
            lambda **kw: _tool_taxonomy(),
        ),
        AgentTool(
            "contract_subtypes",
            "The 25 CUAD agreement families (key, label, description) the sorter classifies into.",
            lambda **kw: _tool_subtypes(),
        ),
        AgentTool(
            "memory",
            "Recent outcomes + stats for this agent (what similar cases taught us).",
            _tool_memory(agent_name),
        ),
    ]
    if agent_name in ("contracts_specialist",):
        tools.append(
            AgentTool(
                "extraction_schema",
                "The ContractExtraction schema fields expected in the output.",
                lambda doc_type="contract", **kw: _tool_schema(doc_type or "contract"),
            )
        )
        tools.append(
            AgentTool(
                "field_types",
                "Per-field deterministic scoring types for a doc class (config-driven).",
                lambda doc_type="contract", **kw: _tool_field_types(doc_type or "contract"),
            )
        )
    return tools


def get_tools(agent_name: str) -> list[AgentTool]:
    """Return the agent's toolkit (built once, cached)."""
    if agent_name not in _TOOLKITS:
        _TOOLKITS[agent_name] = _build_toolkit(agent_name)
    return _TOOLKITS[agent_name]


def render_tools(agent_name: str, max_chars: int = 1500) -> str:
    """Render the agent's tool descriptions as a prompt appendix so the model
    knows what lookups are available (descriptions only — results are fetched
    on demand by the caller when it wants grounded context)."""
    tools = get_tools(agent_name)
    if not tools:
        return ""
    lines = ["## Available tools (read-only lookups)", ""]
    used = 0
    for t in tools:
        line = f"- `{t.name}`: {t.description}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n" + "\n".join(lines) + "\n"
