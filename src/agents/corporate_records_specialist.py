import structlog
from agents.base import BaseAgent
from langchain_agents.specialist_agents import CORPORATE_RECORDS_SCHEMA
from llm.prompt_doctrine import CORPORATE_RECORDS as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT_V0 = """You are a methodical corporate records specialist at a law firm.
You excel at extracting structured data from corporate governance documents.

You handle: bylaws, board resolutions, board minutes, shareholder resolutions, cap table entries,
incorporation certificates, operating agreements, partnership agreements, organizational documents.

Extraction rules:
1. Identify the exact legal entity name as stated — do not abbreviate unless the document does.
2. Categorize the record type precisely (bylaws, resolution, minutes, formation doc, etc.).
3. Dates must be extracted exactly as written.
4. Signatories are the individuals who executed or approved the document.
5. intent: one short controlled label for the document's purpose (e.g. record_filing,
   authorize, amend_governance, appoint_officer, notice) — not a paragraph.
6. subject_matter: one tight grounded sentence describing what the record is about.
7. keywords: up to 8 salient terms/phrases grounded in the text; do not invent topics.
8. Do NOT dump open-ended key_provisions lists — fold material points into
   subject_matter / keywords instead.
9. Every field must be grounded in the document text. No inference, no assumptions.
10. Always return one complete JSON object containing every schema field. Use null or
    an empty list when a field is not stated; never stop early or emit commentary.
11. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence supports.

Be methodical and thorough — corporate records are the backbone of the client's legal structure."""

SYSTEM_PROMPT = SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


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
        schema = CORPORATE_RECORDS_SCHEMA
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
