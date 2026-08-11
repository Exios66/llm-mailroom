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
7. Always return one complete JSON object containing every schema field. Use null or
   an empty list when a field is not stated; never stop early or emit commentary.
8. The `confidence` score must be derived from the evidence in THIS document, not assumed:
   start from the share of schema fields actually found (fields left null lower it), and lower
   it further for uncertain values or truncated input. Never default to a fixed high value
   (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence supports.

Be methodical and thorough — corporate records are the backbone of the client's legal structure."""


class CorporateRecordsSpecialist(BaseAgent):
    agent_name = "corporate_records_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(
        self,
        doc_text: str,
        pages: list[str] | None = None,
        handoff_context: str | None = None,
    ) -> dict:
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
        # Full transcription is ALWAYS the message body (no page content lost);
        # page images are appended additively when the model is vision-capable.
        max_chars = self._configured_max_input_chars()
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"
        if pages:
            truncated += f"\n\n[Attached: {len(pages)} page image(s) of this document — also read them.]"
        user_message = f"Extract structured data from this corporate record:\n\n{truncated}"
        if handoff_context:
            user_message = f"{handoff_context}\n\n{user_message}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("corp_records_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
