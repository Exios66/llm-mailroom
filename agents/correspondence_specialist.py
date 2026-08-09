import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a perceptive correspondence specialist at a law firm.
You read letters, emails, and memos with an eye for subtext, intent, and action items.

You handle: legal correspondence, demand letters, regulatory notices, client communications,
settlement offers, engagement letters, cease-and-desist letters, opinion letters.

Extraction rules:
1. Identify sender, recipient, and any additional recipients (cc'd/copied parties) precisely —
   full names, titles if present, entities.
2. Determine the communication type: letter, email, memo, notice, demand, etc.
3. Key points: preserve every distinct material fact, obligation, breach, demand,
   deadline, remedy, and waiver stated in the communication. Do not compress
   separate contractual terms into a summary that loses a condition or section
   reference. For a demand letter, retain the payment terms, amount, cure
   demand, consequences of nonpayment, and any interest, costs, or fees stated.
4. Demand amount: for demand letters, extract the exact dollar amount demanded
   as a number (e.g. 218440.00 for $218,440.00). Use null when no amount is
   demanded, including memos that merely reference an outstanding balance.
5. Action items: what someone needs to DO as a result of this communication — deadlines included.
6. Urgency: assess tone — is this routine, time-sensitive, or threatening?
   Neutral communications default to "routine" rather than null.
7. Dates are critical — correspondence is often date-sensitive. Use the date the
   communication was sent, not a referenced deadline.
8. Referenced communications: track the narrative thread — list prior letters,
   notices, or communications this message references (e.g. a prior demand letter).

9. Do not infer or embellish facts. Preserve explicit details faithfully; concise
   paraphrases are fine only when they retain the original meaning and conditions.
10. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence supports.

Use the explicit text as the source of truth. Return one complete JSON object with every
schema field; use null for unstated optional values and do not infer urgency from tone alone."""


class CorrespondenceSpecialist(BaseAgent):
    agent_name = "correspondence_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str, pages: list[str] | None = None) -> dict:
        schema = build_structured_schema(
            {
                "sender": {"type": "string", "description": "Who sent the communication"},
                "recipient": {"type": "string", "description": "Who received it"},
                "additional_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cc'd or otherwise copied parties",
                },
                "communication_type": {
                    "type": "string",
                    "description": "Type: letter, email, memo, notice, demand, etc.",
                },
                "communication_date": {
                    "type": ["string", "null"],
                    "description": "Date the communication was sent",
                },
                "key_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Main substantive points made",
                },
                "demand_amount": {
                    "type": ["number", "null"],
                    "description": "Exact dollar amount demanded (demand letters only)",
                },
                "action_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Actions required, with deadlines if stated",
                },
                "urgency": {
                    "type": "string",
                    "description": "Urgency level: routine, time-sensitive, urgent, critical",
                },
                "referenced_communications": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Prior letters, notices, or communications this message references",
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
        user_message = f"Extract structured data from this correspondence:\n\n{truncated}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("correspondence_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
