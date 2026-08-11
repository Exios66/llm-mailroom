"""Model agent for LegalBench runs.

Reuses the vendored ``BaseAgent`` machinery — retry contract (backoff +
jitter, deadline checks, ``max_retries=0``), usage/cost accounting, input
truncation — so LegalBench runs are subject to the same call guarantees as
the pipeline agents. The system prompt comes from ``legalbench/prompts.py``
(versioned; the version is the experiment identity).
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_agents.base_agent import BaseAgent, build_structured_schema


class LegalBenchAgent(BaseAgent):
    """One agent instance per task run; answers via structured JSON."""

    agent_name = "legalbench"

    def __init__(
        self,
        system_prompt: str,
        *,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        max_input_chars: int = 100_000,
        classes: tuple[str, ...] = ("yes", "no"),
    ) -> None:
        super().__init__(model=model, api_key=api_key)
        self._sp = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_input_chars = max_input_chars
        self._classes = tuple(classes)
        # LegalBench is a single-shot task lens: no pipeline memory/skills.
        self._last_usage: Optional[dict[str, Any]] = None

    def system_prompt(self) -> str:
        return self._sp

    def augmented_system_prompt(self, doc_type: str = "") -> str:
        """LegalBench tasks use the task prompt as-is (no sorter skills)."""
        return self._sp

    def answer_binary(self, question: str, document_text: str) -> dict[str, Any]:
        """Yes/no answer with evidence + confidence."""
        schema = build_structured_schema(
            {
                "answer": {"type": "string", "enum": ["yes", "no"]},
                "evidence": {"type": "string"},
                "confidence": {"type": "number"},
            },
            required=["answer", "evidence", "confidence"],
            title="ContractQuestionAnswer",
        )
        user = (
            f"CONTRACT TEXT\n\n{self.truncate_input(document_text)}\n\n"
            f"QUESTION: {question}\n"
            f"Is the answer 'yes' or 'no'? Quote the supporting evidence."
        )
        return self._call_structured(user, schema)

    def classify_family(self, document_text: str) -> dict[str, Any]:
        """One-of-N family classification with confidence."""
        schema = build_structured_schema(
            {
                "family": {"type": "string", "enum": list(self._classes)},
                "reasoning": {"type": "string"},
                "confidence": {"type": "number"},
            },
            required=["family", "reasoning", "confidence"],
            title="ContractFamily",
        )
        user = f"CONTRACT TEXT\n\n{self.truncate_input(document_text)}\n\nCLASSIFY the contract."
        return self._call_structured(user, schema)

    def usage(self) -> dict[str, Any]:
        """Last call's usage/cost ({} when unknown)."""
        usage = self._last_usage or {}
        return {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost": usage.get("cost"),
        }
