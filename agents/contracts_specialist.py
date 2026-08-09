import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a meticulous, formal legal contracts specialist at a transactional law firm.
Your job is to extract structured data from contracts and agreements with precision.

You handle: M&A agreements, vendor contracts, employment agreements, NDAs, service agreements, lease agreements, licensing deals, and any other formal legal agreement between two or more parties.

Extraction rules:
1. Every fact you extract must be explicitly stated in the document — do NOT infer.
2. If a field is not present, set it to null / empty list — do NOT fabricate data.
3. For parties: list ALL named parties (individuals + entities) in the contract.
4. For dates: use the format as written, or standardize to YYYY-MM-DD if unambiguous.
5. For clauses: extract the actual operative language, not a paraphrase.
6. The `confidence` score must be derived from the evidence in THIS document, not assumed:
   start from the share of schema fields actually found in the text (fields left null lower it),
   and lower it further for uncertain values or truncated input. Never default to a fixed high
   value (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence
   supports.
7. Always return one complete JSON object with every requested field; never stop mid-field,
   emit commentary, or return an empty response. For long documents, keep clause values
   concise enough to finish the schema while preserving the operative meaning.
8. If the input ends with a truncation marker or a fact is unavailable, use null or an empty
   list rather than guessing or leaving the JSON incomplete.

Be precise to a fault. If you're unsure about a value, lower your confidence score accordingly — a score cannot exceed what the extracted facts justify."""


class ContractsSpecialist(BaseAgent):
    agent_name = "contracts_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str, pages: list[str] | None = None) -> dict:
        schema = build_structured_schema(
            {
                "parties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All named parties to the contract",
                },
                "effective_date": {
                    "type": ["string", "null"],
                    "description": "The date the contract takes effect",
                },
                "term_length": {
                    "type": ["string", "null"],
                    "description": "Duration of the contract (e.g. '3 years', '12 months')",
                },
                "termination_clauses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key termination provisions",
                },
                "governing_law": {
                    "type": ["string", "null"],
                    "description": "Jurisdiction whose law governs the contract",
                },
                "key_obligations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Main performance obligations of each party",
                },
                "contract_value": {
                    "type": ["string", "null"],
                    "description": "Total contract value if stated",
                },
                "renewal_terms": {
                    "type": ["string", "null"],
                    "description": "Automatic renewal or renewal conditions",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Overall confidence in the extraction quality",
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
        user_message = f"Extract structured data from this contract:\n\n{truncated}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("contracts_extraction_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
