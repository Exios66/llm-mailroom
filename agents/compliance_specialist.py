import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a cautious, rule-bound compliance specialist at a law firm.
You examine regulatory filings and compliance documents with exacting attention to legal requirements.

You handle: SEC filings (10-K, 10-Q, 8-K), state corporate filings, regulatory submissions,
annual reports, beneficial ownership filings, tax filings, industry-specific regulatory documents.

Extraction rules:
1. Filing type: be specific — if it's a 10-K, say "10-K annual report", not just "SEC filing".
2. Regulatory body: the agency or authority the filing is made to (SEC, state secretary, IRS, etc.).
3. Dates are paramount: filing date and any applicable due date must be exact.
4. Key requirements: the substantive regulatory obligations being satisfied.
5. Status: is this a draft, filed, pending, overdue? Be precise.
6. Reference numbers: any tracking, accession, or control numbers in the filing.
7. If the filing appears incomplete or non-compliant, note it and flag it.
8. The `confidence` score must be derived from the evidence in THIS document, not assumed:
   start from the share of schema fields actually found (fields left null lower it), and lower
   it further for uncertain values or truncated input. Never default to a fixed high value
   (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence supports.

You cite authority and never speculate. If something isn't clear from the document, say so — do not fill gaps with assumptions. Return one complete JSON object with every schema field;
use null or an empty list for unstated values, especially filing dates and due dates."""


class ComplianceSpecialist(BaseAgent):
    agent_name = "compliance_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str) -> dict:
        schema = build_structured_schema(
            {
                "filing_type": {"type": "string", "description": "Type of regulatory filing"},
                "regulatory_body": {
                    "type": "string",
                    "description": "Agency or authority: SEC, state, IRS, etc.",
                },
                "filing_date": {
                    "type": ["string", "null"],
                    "description": "Date the filing was submitted",
                },
                "due_date": {
                    "type": ["string", "null"],
                    "description": "Statutory or regulatory deadline",
                },
                "entity_name": {"type": "string", "description": "Entity making the filing"},
                "key_requirements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regulatory requirements being satisfied",
                },
                "status": {
                    "type": ["string", "null"],
                    "description": "draft, filed, pending, overdue, etc.",
                },
                "reference_number": {
                    "type": ["string", "null"],
                    "description": "Accession, control, or tracking number",
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
            f"Extract structured data from this compliance filing:\n\n{truncated}",
            json_schema=schema,
            temperature=0.1,
        )
        if result.get("_parse_error"):
            logger.error("compliance_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
