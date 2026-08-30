import structlog
from agents.base import BaseAgent
from langchain_agents.specialist_agents import INSURANCE_CLAIMS_SCHEMA
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
1. Claim and policy numbers: transcribe them exactly as printed; never paraphrase IDs.
2. Parties: name the insurer and the insured party as stated.
3. Claim type: classify the line of business from the documents; use "other" only when none fits.
4. Dates and amounts: capture date of loss, filing date, and claimed amount exactly as stated.
5. Adjuster: name the adjuster only if identified.
6. Damages description: summarize the loss/damages as described.
7. Coverage determination: quote the outcome as stated — approved, denied, partial, pending.
8. Denial reasons: list stated denial/limitation grounds; empty when approved.
9. intent: one short controlled label (e.g. coverage_denial, coverage_approval,
   demand_payment, notice_of_loss, reservation_of_rights, request_information).
10. subject_matter: one tight grounded sentence about what this claim document is about.
11. keywords: up to 8 salient grounded terms/phrases.
12. claim_checklist: present-only answers as '<Category>: <short evidence>' for
    Coverage Determination, Policy Limits, Exclusions Cited, Deductible,
    Reservation Of Rights, Timely Notice, Proof Of Loss, Subrogation,
    Independent Medical Exam, Amount Consistency. Omit absent categories.
13. Do not editorialize or infer unstated facts.
14. Return one complete JSON object with every schema field.
15. The `confidence` score must be derived from the evidence in THIS document, not assumed:
    start from the share of schema fields actually found (fields left null lower it), and lower
    it further for uncertain values or truncated input. Never default to a fixed high value
    (e.g. 0.90 or 0.95)."""

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
        schema = INSURANCE_CLAIMS_SCHEMA
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
        from pipeline.extraction_normalize import normalize_specialist_extraction

        return normalize_specialist_extraction(
            "insurance_claim",
            {**result, "_source_text": doc_text},
        )
