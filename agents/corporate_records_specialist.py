import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a methodical corporate records specialist at a law firm.
You excel at extracting structured data from corporate governance documents.

You handle: bylaws, board resolutions, board minutes, shareholder resolutions, cap table entries,
incorporation certificates, operating agreements, partnership agreements, organizational documents.

Extraction rules:
1. Identify the exact legal entity name as stated — do not abbreviate unless the document does.
2. Categorize the record type precisely (bylaws, resolution, minutes, formation doc, etc.).
3. Dates must be extracted exactly as written.
4. Key provisions should capture the operative governance language.
5. Signatories are the individuals who executed or approved the document.
6. Every field must be grounded in the document text. No inference, no assumptions.

Be methodical and thorough — corporate records are the backbone of the client's legal structure."""


class CorporateRecordsSpecialist(BaseAgent):
    agent_name = "corporate_records_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str) -> dict:
        schema = build_structured_schema(
            {
                "entity_name": {"type": "string", "description": "Legal entity name"},
                "record_type": {
                    "type": "string",
                    "description": "Type of record: bylaws, resolution, minutes, formation, etc.",
                },
                "effective_date": {
                    "type": ["string", "null"],
                    "description": "Date the record took effect",
                },
                "key_provisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key governance provisions",
                },
                "signatories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Individuals who signed or approved",
                },
                "jurisdiction": {
                    "type": ["string", "null"],
                    "description": "State/country of incorporation",
                },
                "filing_number": {
                    "type": ["string", "null"],
                    "description": "Official filing or document reference number",
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
            f"Extract structured data from this corporate record:\n\n{truncated}",
            json_schema=schema,
            temperature=0.1,
        )
        if result.get("_parse_error"):
            logger.error("corp_records_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
