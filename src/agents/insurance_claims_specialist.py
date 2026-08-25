import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompt_doctrine import INSURANCE_CLAIMS as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT_V0 = """You are a meticulous insurance-claims specialist at a law firm.
You read insurance claim documentation — FNOL forms, adjuster reports and estimates,
demand packages, coverage determinations, reservation-of-rights letters, denial
letters, and EOB statements — and distill their claim facts.

You handle: first-party and third-party claims across auto, property, liability,
health, life, and workers' compensation lines; both open claims and final
determinations.

Extraction rules:
1. Claim and policy numbers: transcribe them exactly as printed (claim no., policy
   no., FNOL reference); these are identifiers, never paraphrase them.
2. Parties: name the insurer and the insured party as stated on the documents.
3. Claim type: classify the line of business (auto, property, liability, health,
   life, workers_comp) from the documents themselves; use "other" only when none fits.
4. Dates and amounts: capture date of loss, filing date, and claimed amount exactly
   as stated; do not compute or convert amounts.
5. Adjuster: name the adjuster only if the documents identify one.
6. Damages description: summarize the loss/damages as described by the documents.
7. Coverage determination: quote the outcome as stated — approved, denied, partial,
   pending — never infer a determination that is not written.
8. Denial reasons: list stated denial/limitation grounds distinctly; if the claim was
   approved, leave this empty.
9. Do not editorialize and do not infer unstated facts — report what the documents state.
10. Return one complete JSON object with every schema field. Use null or an empty list
    for facts not stated; never infer a claim number, policy number, date, amount, or
    determination.
11. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95) — use the full 0.0-1.0 range and pick the number the evidence supports."""

SYSTEM_PROMPT = SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


class InsuranceClaimsSpecialist(BaseAgent):
    agent_name = "insurance_claims_specialist"

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
                "claim_number": {"type": ["string", "null"], "description": "Claim number exactly as printed (CLAIM NO., FNOL ref.)"},
                "policy_number": {"type": ["string", "null"], "description": "Policy number exactly as printed"},
                "insurer": {"type": "string", "description": "Named insurance company / carrier"},
                "insured_party": {"type": "string", "description": "Named insured or claimant"},
                "claim_type": {"type": "string", "description": "Line of business: auto, property, liability, health, life, workers_comp, other"},
                "date_of_loss": {"type": ["string", "null"], "description": "Date the loss/event occurred, if stated"},
                "date_filed": {"type": ["string", "null"], "description": "Date the claim was filed, if stated"},
                "claimed_amount": {"type": ["number", "null"], "description": "Amount claimed/demanded in USD, if stated"},
                "adjuster": {"type": ["string", "null"], "description": "Named adjuster handling the claim, if stated; null when absent"},
                "damages_description": {"type": "string", "description": "Summary of the loss/damages as described"},
                "coverage_determination": {"type": "string", "description": "Outcome as stated: approved, denied, partial, pending"},
                "denial_reasons": {"type": "array", "items": {"type": "string"}, "description": "Stated denial/limitation grounds, if denied"},
                "supporting_documents": {"type": "array", "items": {"type": "string"}, "description": "Referenced supporting documents (police report, receipts, medical records, etc.)"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
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
        user_message = f"Extract structured data from this insurance claim documentation:\n\n{truncated}"
        if handoff_context:
            user_message = f"{handoff_context}\n\n{user_message}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("insurance_claim_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
