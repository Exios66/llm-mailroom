import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a perceptive correspondence specialist at a law firm.
You read letters, emails, and memos with an eye for subtext, intent, and action items.

You handle: legal correspondence, demand letters, regulatory notices, client communications,
settlement offers, engagement letters, cease-and-desist letters, opinion letters.

Extraction rules:
1. Identify sender and recipient precisely — full names, titles if present, entities.
2. Determine the communication type: letter, email, memo, notice, demand, etc.
3. Key points: preserve every distinct material fact, obligation, breach, demand,
   deadline, remedy, and waiver stated in the communication. Do not compress
   separate contractual terms into a summary that loses a condition or section
   reference. For a demand letter, retain the payment terms, amount, cure
   demand, consequences of nonpayment, and any interest, costs, or fees stated.
4. Action items: what someone needs to DO as a result of this communication — deadlines included.
5. Urgency: assess tone — is this routine, time-sensitive, or threatening?
6. Dates are critical — correspondence is often date-sensitive.
7. Track narrative: if this letter references prior communications, note the thread.

8. Do not infer or embellish facts. Preserve explicit details faithfully; concise
   paraphrases are fine only when they retain the original meaning and conditions.

Use the explicit text as the source of truth."""


class CorrespondenceSpecialist(BaseAgent):
    agent_name = "correspondence_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str) -> dict:
        schema = build_structured_schema(
            {
                "sender": {"type": "string", "description": "Who sent the communication"},
                "recipient": {"type": "string", "description": "Who received it"},
                "date_sent": {
                    "type": ["string", "null"],
                    "description": "Date the communication was sent",
                },
                "subject": {"type": "string", "description": "Subject line or topic"},
                "communication_type": {
                    "type": "string",
                    "description": "Type: letter, email, memo, notice, demand, etc.",
                },
                "key_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Main substantive points made",
                },
                "action_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Actions required, with deadlines if stated",
                },
                "urgency": {
                    "type": ["string", "null"],
                    "description": "Urgency level: routine, time-sensitive, urgent, critical",
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
            f"Extract structured data from this correspondence:\n\n{truncated}",
            json_schema=schema,
            temperature=0.1,
        )
        if result.get("_parse_error"):
            logger.error("correspondence_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
