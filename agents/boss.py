import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

BOSS_SYSTEM_PROMPT = """You are the Boss — the calm, authoritative operational overseer of a legal document processing pipeline.

Your personality is the same whether you're adjudicating a single document's conflict or monitoring
the entire system: calm under pressure, data-driven, decisive. You make the judgment call when
the specialists disagree or the system encounters ambiguity it can't resolve alone.

=== IN-GRAPH ROLE (escalation adjudication) ===
When a document's extraction conflicts with existing matter data, or a document has failed
classification/extraction twice, you are called to adjudicate. You receive:
- The document's manifest (classification, extraction, confidence scores)
- Relevant matter context (existing catalog entries for the same matter)

Your decision options:
1. Resolve directly — you determine the correct classification/extraction and the document proceeds.
2. Escalate to human review — the situation is genuinely ambiguous and needs a person.

You must return a decision: either "approved" (resolved, proceed to compile_report) or "review"
(route to human review), along with your reasoning.

=== OPS-MONITOR ROLE (system-wide sweep) ===
In this role you analyze aggregate metrics across all documents. You consider:
- Stuck documents (stale in PROCESSING for too long)
- Error-rate spikes for specific document types
- Review backlog growing
- Systemic patterns the per-document agents cannot see

Your decisions in this role are: log an alert, or recommend pausing ingestion.

In both roles: be decisive, be transparent about your reasoning, and err on the side of caution
when the data is genuinely ambiguous."""


class BossAgent(BaseAgent):
    agent_name = "boss"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, BOSS_SYSTEM_PROMPT)
        return text

    def adjudicate(
        self,
        manifest_data: dict,
        matter_context: list[dict] | None = None,
    ) -> dict:
        context_str = "No existing matter records found."
        if matter_context:
            records = "\n".join(
                f"  - {r.get('doc_type', 'unknown')}: {r.get('extracted_data', {})}"
                for r in matter_context
            )
            context_str = f"Existing matter records:\n{records}"

        user_message = f"""ADJUDICATION REQUEST:

Document manifest:
- doc_id: {manifest_data.get('doc_id')}
- doc_type: {manifest_data.get('doc_type')}
- classification_confidence: {manifest_data.get('classification_confidence')}
- extraction_confidence: {manifest_data.get('extraction_confidence')}
- extracted_data: {manifest_data.get('extracted_data')}
- escalation_reason: {manifest_data.get('escalation_reason')}

{context_str}

Decide: "approved" (resolve and proceed) or "review" (escalate to human)."""

        schema = build_structured_schema(
            {
                "decision": {
                    "type": "string",
                    "enum": ["approved", "review"],
                },
                "reasoning": {"type": "string"},
                "resolution_notes": {"type": "string"},
            }
        )
        result = self._call_structured(user_message, json_schema=schema, temperature=0.2)
        if result.get("_parse_error"):
            logger.error("boss_parse_error")
            return {"decision": "review", "reasoning": "parse error — defaulting to review"}
        return result

    def analyze_system_metrics(self, metrics: dict) -> dict:
        user_message = f"""SYSTEM METRICS ANALYSIS:

{metrics}

Analyze these pipeline-wide metrics. Identify:
1. Any systemic issues (error spikes, stuck documents, backlogs)
2. Severity assessment (info, warning, critical)
3. Recommended action (none, alert, pause_ingestion)"""

        schema = build_structured_schema(
            {
                "assessment": {"type": "string"},
                "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                "recommended_action": {
                    "type": "string",
                    "enum": ["none", "alert", "pause_ingestion"],
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            }
        )
        result = self._call_structured(user_message, json_schema=schema, temperature=0.2)
        if result.get("_parse_error"):
            return {"severity": "warning", "recommended_action": "alert", "findings": ["metrics analysis failed"]}
        return result
