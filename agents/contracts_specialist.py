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
6. Produce a confidence score reflecting how certain you are about the overall extraction quality.

Be precise to a fault. If you're unsure about a value, lower your confidence score accordingly."""


class ContractsSpecialist(BaseAgent):
    agent_name = "contracts_specialist"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, doc_text: str) -> dict:
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
        max_chars = self._configured_max_input_chars()
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"

        result = self._call_structured(
            f"Extract structured data from this contract:\n\n{truncated}",
            json_schema=schema,
            temperature=0.1,
        )
        if result.get("_parse_error"):
            logger.error("contracts_extraction_parse_error")
            return {"confidence": 0.3, "_parse_error": True}
        return result
