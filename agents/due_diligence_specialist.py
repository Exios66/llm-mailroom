import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a skeptical due diligence specialist at a transactional law firm.
You scrutinize documents for risks, inconsistencies, and material findings.

You handle: due diligence checklists, disclosure schedules, diligence memoranda, risk assessments,
vendor due diligence reports, background check summaries, financial review documents.

Extraction rules:
1. Material findings: These are significant facts discovered during diligence — list each one.
2. Risk flags: Issues that could affect the transaction — regulatory concerns, litigation exposure,
   financial irregularities, compliance gaps, key-person dependencies, IP issues.
3. Outstanding items: Open questions or documents still needed to complete diligence.
4. Be aggressive about flagging risks — it's better to over-flag than miss something.
5. Confidence reflects how thoroughly you believe the document covers its subject matter.
6. If the document is incomplete or the diligence appears superficial, note it and lower confidence.

You are told to be skeptical for good reason — the client depends on finding problems before they become liabilities."""


class DueDiligenceSpecialist(BaseAgent):
    agent_name = "due_diligence_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str) -> dict:
        schema = build_structured_schema(
            {
                "target_entity": {"type": "string", "description": "Entity being investigated"},
                "diligence_type": {
                    "type": "string",
                    "description": "Type of diligence: financial, legal, operational, etc.",
                },
                "material_findings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Significant facts discovered",
                },
                "risk_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Identified risks and concerns",
                },
                "outstanding_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Open questions or needed documents",
                },
                "document_date": {
                    "type": ["string", "null"],
                    "description": "Date of the diligence document",
                },
                "prepared_by": {
                    "type": ["string", "null"],
                    "description": "Who prepared or authored the document",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            }
        )
        max_chars = self._configured_max_input_chars()
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"

        result = self._call_structured(
            f"Extract structured data from this due diligence document:\n\n{truncated}",
            json_schema=schema,
            temperature=0.1,
        )
        if result.get("_parse_error"):
            logger.error("dd_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
