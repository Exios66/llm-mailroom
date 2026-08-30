import structlog
from agents.base import BaseAgent
from langchain_agents.specialist_agents import CORRESPONDENCE_SCHEMA
from llm.prompt_doctrine import CORRESPONDENCE as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT_V0 = """You are a perceptive correspondence specialist at a law firm.
You read letters, emails, and memos with an eye for subtext, intent, and action items.

You handle: legal correspondence, demand letters, regulatory notices, client communications,
settlement offers, engagement letters, cease-and-desist letters, opinion letters.

Extraction rules:
1. Identify sender, recipient, and any additional recipients (cc'd/copied parties) precisely —
   full names, titles if present, entities.
2. Determine the communication type: letter, email, memo, notice, demand, etc.
3. intent: one short controlled label (e.g. demand_payment, notice, request_information,
   threaten_litigation, acknowledge, schedule_meeting).
4. subject_matter: one tight grounded sentence about what the communication is about.
5. keywords: up to 8 salient terms/phrases grounded in the text; do not invent topics.
6. action_items: at most 3 concrete actions with deadlines if stated.
7. Demand amount: for demand letters, extract the exact dollar amount demanded
   as a number (e.g. 218440.00 for $218,440.00). Use null when no amount is demanded.
8. Press releases and wire-service articles: set recipient to null when there is no
   named addressee. Use the issuing company or media-contact line as sender when needed.
9. Urgency: routine, time-sensitive, urgent, or critical. Neutral defaults to "routine".
10. Dates are critical — use the date the communication was sent, not a referenced deadline.
11. Do NOT dump long key_points lists — use intent / subject_matter / keywords instead.
12. Do not infer or embellish facts.
13. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95).

Use the explicit text as the source of truth. Return one complete JSON object with every
schema field; use null for unstated optional values."""

SYSTEM_PROMPT = SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


class CorrespondenceSpecialist(BaseAgent):
    agent_name = "correspondence_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(
        self,
        doc_text: str,
        pages: list[str] | None = None,
        handoff_context: str | None = None,
    ) -> dict:
        schema = CORRESPONDENCE_SCHEMA
        max_chars = self._configured_max_input_chars()
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"
        if pages:
            truncated += f"\n\n[Attached: {len(pages)} page image(s) of this document — also read them.]"
        user_message = f"Extract structured data from this correspondence:\n\n{truncated}"
        if handoff_context:
            user_message = f"{handoff_context}\n\n{user_message}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("correspondence_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        from pipeline.extraction_normalize import normalize_specialist_extraction

        return normalize_specialist_extraction("correspondence", result)
