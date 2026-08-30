"""Arbiter agent — Lane B judgment arbitration (KANBAN-063).

Architecture alignment: the general pipeline gives the Judge an Arbiter
exception path. When the in-pipeline judge verdict fails (incomplete /
inaccurate extraction), the arbiter — not the raw pipeline — decides the
outcome. It sees the specialist extraction AND the judge's findings, and is
constrained to three bounded decisions:

- ``accept_with_caveats`` — extraction is materially sound; proceed to
  compile_report with the judge's findings recorded.
- ``retry_extraction`` — specific, recoverable field failures; the graph
  bounds the retries (``arbiter_retry_count``) and escalates when exhausted.
- ``human_review`` — genuinely ambiguous or beyond repair; escalate.

Profile registered upstream as ``arbiter`` (llm-dojo-scoring v0.6.0, audit
bundle, ground-truth-free).
"""

import json

import structlog

from agents.base import BaseAgent, build_structured_schema
from llm.prompt_doctrine import ARBITER as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

ARBITER_SYSTEM_PROMPT_V0 = """You are the Arbiter — the final judgment authority for a legal-document
extraction pipeline. When the quality judge rejects an extraction, you decide
what happens next. You are calm, evidence-driven, and decisive.

You receive: the document type, the specialist's extracted fields, and the
judge's verdict with concrete findings.

Your decision options (choose exactly one):
1. "accept_with_caveats" — the extraction is materially sound. The judge's
   complaints are cosmetic (formatting variants, conservative completeness
   calls, fields genuinely absent from the visible source). The pipeline
   proceeds with the extraction; your caveats are recorded in the audit log
   (event arbiter_decided) and copied onto the archived or review/failed
   manifest for the archivist.
2. "retry_extraction" — a small, named set of fields failed for a recoverable
   reason (missed evidence the source actually contains, wrong format, a
   dropped list item). List the specific fields to fix. The pipeline re-runs
   extraction with your findings attached; retries are bounded.
3. "human_review" — the document is genuinely ambiguous, the source is
   unreadable/truncated in a material way, or failures compound beyond a
   bounded retry. Escalate to a human with a precise handoff summary.

Rules:
1. Judge only registered schema fields; ignore keys beginning with underscore
   (pipeline metadata).
2. Treat the judge's findings as evidence, not as binding truth — you may
   overrule a conservative judge when the extraction is defensible. Cite why.
3. Do not invent facts. If evidence is insufficient, that is human_review.
4. Be decisive: default to the least destructive sufficient action.
5. Return one complete JSON object matching the requested schema and no extra
   text."""

ARBITER_SYSTEM_PROMPT = ARBITER_SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


class ArbiterAgent(BaseAgent):
    """Judgment arbitration on failed judge verdicts."""

    agent_name = "arbiter"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, ARBITER_SYSTEM_PROMPT)
        return text

    def arbitrate(
        self,
        doc_type: str,
        extracted: dict,
        judge_verdict: str,
        judge_findings: list[str] | str,
        judge_score: float | None = None,
    ) -> dict:
        """Decide the outcome for a judge-rejected extraction.

        Returns ``{decision, fields_to_fix, reasoning, handoff_summary}``
        where decision is one of ``accept_with_caveats``, ``retry_extraction``,
        ``human_review``.
        """
        findings_text = (
            "\n".join(f"- {f}" for f in judge_findings)
            if isinstance(judge_findings, list)
            else str(judge_findings)
        )
        user_message = (
            f"DOCUMENT TYPE: {doc_type}\n\n"
            f"JUDGE VERDICT: {judge_verdict}\n"
            + (f"JUDGE SCORE: {judge_score}\n" if judge_score is not None else "")
            + "\nJUDGE FINDINGS\n"
            f"{findings_text}\n\n"
            "SPECIALIST EXTRACTION\n"
            f"{json.dumps(extracted, ensure_ascii=False, indent=2, default=str)}"
        )
        schema = build_structured_schema(
            {
                "decision": {
                    "type": "string",
                    "enum": [
                        "accept_with_caveats",
                        "retry_extraction",
                        "human_review",
                    ],
                },
                "fields_to_fix": {"type": "array", "items": {"type": "string"}},
                "reasoning": {"type": "string"},
                "handoff_summary": {"type": "string"},
            },
            required=["decision", "fields_to_fix", "reasoning", "handoff_summary"],
        )
        result = self._call_structured(user_message, schema)
        decision = result.get("decision")
        if decision not in ("accept_with_caveats", "retry_extraction", "human_review"):
            # Guardrail: an off-schema decision must never drive routing —
            # treat it as an escalation (fail safe toward human eyes).
            logger.warning(
                "arbiter_invalid_decision",
                decision=str(decision)[:80],
            )
            result["decision"] = "human_review"
            result["reasoning"] = f"arbiter returned invalid decision {decision!r}; escalated"
        logger.info("arbiter_decision", decision=result.get("decision"))
        return result
