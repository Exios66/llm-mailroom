"""LLM-as-a-judge that evaluates extraction completeness against the source
document. Used offline by scripts/run_completeness_judge.py; never runs inside
the pipeline."""

import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt
from pipeline.config import load_config
from schemas.documents import get_extraction_schema

logger = structlog.get_logger(__name__)

LABELS = ["complete", "partial", "incomplete"]

CLASSIFICATION_LABELS = ["correct", "incorrect", "ambiguous"]

CORRECTNESS_LABELS = ["accurate", "partial", "inaccurate"]

SYSTEM_PROMPT = """You are an expert legal reviewer evaluating the completeness of an automated
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

CLASSIFICATION_SYSTEM_PROMPT = """You are an expert legal reviewer auditing an automated document-classification
pipeline. The pipeline's task specification (defined in config/taxonomy.yaml) is to assign every
incoming document exactly one of the available legal document classes.

Your job: given a document, the class the pipeline assigned, and the classifier's stated reasoning,
judge whether that assignment matches the task specification.

Rules:
1. A class is CORRECT if the document clearly fits it best, even if another class also plausibly fits.
2. A class is INCORRECT if a different available class fits the document better.
3. AMBIGUOUS is reserved for documents that genuinely span multiple classes with no clear best fit.
4. classification_quality is a 0-1 float: 1.0 = clearly and unambiguously correct.
5. In reasoning, cite the evidence in the document that supports or contradicts the assignment."""

CORRECTNESS_SYSTEM_PROMPT = """You are an expert legal reviewer auditing the factual accuracy of an automated
document-extraction run against the source document.

Your job: verify that every extracted field value is grounded in the document text — no
fabrication, no paraphrase that changes meaning, no values pulled from thin air.

Rules:
1. ACCURATE: every populated field is supported by the document text and correct.
2. PARTIAL: most values are correct, but at least one value is wrong, overstated, or unsupported.
3. INACCURATE: multiple values are fabricated, materially wrong, or key required fields are wrong.
4. extraction_correctness is a 0-1 float (1.0 = fully accurate).
5. Empty fields are not errors by themselves — absence of wrong data is neutral.
6. In reasoning, name the specific fabricated or wrong values you found."""


class CompletenessJudge(BaseAgent):
    agent_name = "judge"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

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

    @staticmethod
    def _truncate(doc_text: str, max_chars: int = 16000) -> str:
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total characters ...]"
        return truncated

    @staticmethod
    def _taxonomy_spec() -> str:
        """Render the task specification (taxonomy doc classes) for the judge."""
        cfg = load_config()
        lines = []
        for d in cfg.get("doc_classes", []):
            label = d.get("label", d["key"])
            desc = d.get("description", "")
            lines.append(f"  - {d['key']} ({label}): {desc}")
        return "\n".join(lines) or "(no doc classes configured)"

    def judge_classification(
        self,
        doc_type: str,
        doc_text: str,
        reasoning: str = "",
    ) -> dict:
        """Judge whether the sorter's assigned class matches the taxonomy task
        specification (usable on production traces with no ground truth)."""
        schema = build_structured_schema(
            {
                "classification_correct": {
                    "type": "string",
                    "enum": CLASSIFICATION_LABELS,
                    "description": "Does the assigned class match the task specification?",
                },
                "classification_quality": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "1.0 = clearly and unambiguously correct",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Evidence in the document supporting or contradicting the assignment",
                },
            }
        )
        user_message = f"""Audit the classification assignment against the task specification.

Task specification (available document classes):
{self._taxonomy_spec()}

Assigned classification: {doc_type}
Classifier reasoning: {reasoning or 'none provided'}

Document text:
--- BEGIN TEXT ---
{self._truncate(doc_text)}
--- END TEXT ---"""

        variant_prompt, self._langfuse_prompt = get_managed_prompt(
            "judge-classification", CLASSIFICATION_SYSTEM_PROMPT
        )
        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.0,
            system_prompt=variant_prompt,
        )
        if result.get("_parse_error"):
            logger.error("judge_classification_parse_error", doc_type=doc_type)
            return {
                "classification_correct": "ambiguous",
                "classification_quality": 0.0,
                "reasoning": "judge output failed to parse",
            }
        label = result.get("classification_correct", "ambiguous")
        if label not in CLASSIFICATION_LABELS:
            label = "ambiguous"
        try:
            quality = float(result.get("classification_quality", 0.0))
        except (TypeError, ValueError):
            quality = 0.0
        return {
            "classification_correct": label,
            "classification_quality": max(0.0, min(1.0, quality)),
            "reasoning": str(result.get("reasoning", "")),
        }

    def judge_extraction_correctness(
        self,
        doc_type: str,
        extracted: dict,
        doc_text: str,
    ) -> dict:
        """Judge whether the extracted field values are factually accurate
        (no fabrication) against the source document text."""
        schema = build_structured_schema(
            {
                "extraction_correctness": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "1.0 = every populated field is supported by the document",
                },
                "extraction_correctness_label": {
                    "type": "string",
                    "enum": CORRECTNESS_LABELS,
                    "description": "Overall factual accuracy of the extraction",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Specific fabricated or wrong values found",
                },
            }
        )
        user_message = f"""Audit the factual accuracy of the extraction.

Document type: {doc_type}

Extracted data:
{extracted}

Source document text:
--- BEGIN TEXT ---
{self._truncate(doc_text)}
--- END TEXT ---"""

        variant_prompt, self._langfuse_prompt = get_managed_prompt(
            "judge-correctness", CORRECTNESS_SYSTEM_PROMPT
        )
        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.0,
            system_prompt=variant_prompt,
        )
        if result.get("_parse_error"):
            logger.error("judge_correctness_parse_error", doc_type=doc_type)
            return {
                "extraction_correctness": 0.0,
                "extraction_correctness_label": "inaccurate",
                "reasoning": "judge output failed to parse",
            }
        label = result.get("extraction_correctness_label", "partial")
        if label not in CORRECTNESS_LABELS:
            label = "partial"
        try:
            correctness = float(result.get("extraction_correctness", 0.0))
        except (TypeError, ValueError):
            correctness = 0.0
        return {
            "extraction_correctness": max(0.0, min(1.0, correctness)),
            "extraction_correctness_label": label,
            "reasoning": str(result.get("reasoning", "")),
        }
