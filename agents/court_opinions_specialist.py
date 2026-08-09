import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a meticulous judicial-opinion specialist at a law firm.
You read published court opinions, orders, and rulings and distill their holdings.

You handle: published decisions and memorandum opinions, orders on motions,
rulings from federal district/circuit courts, state courts, and administrative
law judges.

Extraction rules:
1. Case name: the style of the case (e.g. "Smith v. Jones"), parties in the caption.
2. Court and date: identify the issuing court and decision date precisely.
3. Opinion type: published opinion, unpublished/memorandum opinion, order, per
   curiam, en banc, dissent, etc.
4. Holding: the court's actual holding — the rule of law the case establishes.
5. Legal issues: the questions of law presented; list each distinctly.
6. Outcome: what the court decided — affirmed, reversed, remanded, denied, granted.
7. Citations: reporter citations and docket numbers if present.
8. Do not editorialize — report what the court held, not your own view of the law.
9. Return one complete JSON object with every schema field. Use null or an empty list
   for facts not stated; never infer a case name, date, docket, author, or citation.
10. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence supports."""


class CourtOpinionsSpecialist(BaseAgent):
    agent_name = "court_opinions_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str, pages: list[str] | None = None) -> dict:
        schema = build_structured_schema(
            {
                "case_name": {"type": "string", "description": "Style of the case, e.g. Smith v. Jones"},
                "court": {"type": "string", "description": "Issuing court, e.g. U.S. Court of Appeals for the Ninth Circuit"},
                "date_decided": {
                    "type": ["string", "null"],
                    "description": "Date the decision was issued",
                },
                "docket_number": {
                    "type": ["string", "null"],
                    "description": "Case/docket number if present",
                },
                "opinion_type": {
                    "type": "string",
                    "description": "Published opinion, memorandum, per curiam, order, etc.",
                },
                "parties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Named parties from the caption",
                },
                "holding": {
                    "type": "string",
                    "description": "The court's holding — the rule of law established",
                },
                "legal_issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Questions of law presented and decided",
                },
                "outcome": {
                    "type": "string",
                    "description": "Disposition: affirmed, reversed, remanded, denied, granted",
                },
                "citations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reporter citations and docket numbers",
                },
                "authored_by": {
                    "type": ["string", "null"],
                    "description": "Judge who authored the opinion, if stated",
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
        user_message = f"Extract structured data from this court opinion:\n\n{truncated}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("court_opinion_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
