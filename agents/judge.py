"""LLM-as-a-judge that evaluates extraction completeness against the source
document. Used offline by scripts/run_completeness_judge.py; never runs inside
the pipeline."""

import structlog
from agents.base import BaseAgent, build_structured_schema
from schemas.documents import get_extraction_schema

logger = structlog.get_logger(__name__)

LABELS = ["complete", "partial", "incomplete"]


class CompletenessJudge(BaseAgent):
    agent_name = "judge"

    def system_prompt(self) -> str:
        return """You are an expert legal reviewer evaluating the completeness of an automated
document-extraction run. You compare what a specialist agent extracted against the source
document text and judge whether anything the document states was missed or fabricated.

Rules:
1. A field is COMPLETE if the document states the information and the extraction captured it.
2. A field is MISSING if the document states the information but the extraction left it empty.
3. A field is FABRICATED if the extraction reports information the document does not contain.
4. Judge only fields the schema asks for. Empty arrays/null for genuinely absent info are fine.
5. Score completeness as the fraction of expected fields that were correctly captured.
6. Assign 'complete' when completeness >= 0.95, 'partial' when >= 0.5, else 'incomplete'.
7. In reasoning, list the specific gaps or fabrications you found."""

    @staticmethod
    def _field_list(doc_type: str) -> str:
        model = get_extraction_schema(doc_type)
        if model is None:
            return "(no schema registered)"
        lines = []
        for name, field in model.model_fields.items():
            ann = str(field.annotation).replace("typing.", "")
            desc = field.description or ""
            lines.append(f"  - {name}: {ann}{': ' + desc if desc else ''}")
        return "\n".join(lines)

    def judge_completeness(
        self,
        doc_type: str,
        extracted: dict,
        doc_text: str,
    ) -> dict:
        schema = build_structured_schema(
            {
                "completeness": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Fraction of expected fields correctly captured",
                },
                "completeness_label": {
                    "type": "string",
                    "enum": LABELS,
                    "description": "complete >= 0.95, partial >= 0.5, else incomplete",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Specific gaps or fabrications found",
                },
            }
        )
        max_chars = 16000
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total characters ...]"

        user_message = f"""Evaluate extraction completeness.

Document type: {doc_type}

Expected extraction fields:
{self._field_list(doc_type)}

Extracted data:
{extracted}

Source document text:
--- BEGIN TEXT ---
{truncated}
--- END TEXT ---"""

        result = self._call_structured(user_message, json_schema=schema, temperature=0.0)
        if result.get("_parse_error"):
            logger.error("judge_parse_error", doc_type=doc_type)
            return {
                "completeness": 0.0,
                "completeness_label": "incomplete",
                "reasoning": "judge output failed to parse",
            }
        label = result.get("completeness_label", "incomplete")
        if label not in LABELS:
            label = "incomplete"
        return {
            "completeness": float(result.get("completeness", 0.0)),
            "completeness_label": label,
            "reasoning": str(result.get("reasoning", "")),
        }
