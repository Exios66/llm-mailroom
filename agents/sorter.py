import json
import structlog
from agents.base import BaseAgent, build_structured_schema
from llm.prompts import get_managed_prompt
from pipeline.config import get_all_doc_types, load_config

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a fast, decisive legal document classifier operating in a transactional/corporate law firm's mailroom. Your job is to rapidly identify what kind of legal document you're looking at.

Available document classes:
{{doc_type_descriptions}}

Rules:
1. Read the document quickly — you should classify within seconds.
2. Derive the confidence from the evidence in THIS document: how strongly the format and
   content match one class, and whether signals of other classes are present. Use the full
   0.0-1.0 range — never default to a fixed high value (e.g. 0.90 or 0.95) merely because a
   document "looks normal"; the score must correspond to the evidence, not a habit.
3. If the document clearly matches one class with no competing-class signals, a high score
   (0.90+) is acceptable ONLY when the reasoning cites the concrete evidence for that class
   and the absence of competing signals.
4. If the document spans multiple categories or is ambiguous, pick the best fit and assign
   proportionally lower confidence (roughly 0.50-0.85, lower as ambiguity increases).
5. If you genuinely cannot determine the type, set confidence low (below 0.50) and explain why.
6. Do NOT guess wildly — flag ambiguity instead of committing to a wrong classification.
7. Classify the document's substantive form, not the source wrapper or filing context:
   a judicial decision is a court_opinion even when it discusses a contract, and a
   demand letter is correspondence even when it enforces a contract.

Return a JSON object with:
- doc_type: one of the available class keys listed above
- confidence: float between 0.0 and 1.0, derived from the evidence — the reasoning must
  justify the exact value chosen (format match, competing classes, missing or truncated text)
- reasoning: short explanation of your classification decision. Return one complete
  JSON object and no preamble or trailing text."""


class SorterAgent(BaseAgent):
    agent_name = "sorter"

    def system_prompt(self) -> str:
        cfg = load_config()
        doc_types = cfg.get("doc_classes", [])
        type_descriptions = "\n".join(
            f"  - {d['key']}: {d.get('label', d['key'])} — {d.get('description', '')}"
            for d in doc_types
        )
        text, self._langfuse_prompt = get_managed_prompt(
            self.agent_name,
            SYSTEM_PROMPT,
            {"doc_type_descriptions": type_descriptions},
        )
        return text

    def classify(self, doc_text: str, pages: list[str] | None = None) -> tuple[str, float, str]:
        schema = build_structured_schema(
            {
                "doc_type": {"type": "string", "enum": get_all_doc_types()},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reasoning": {"type": "string"},
            }
        )
        # The full (budget-truncated) transcription is ALWAYS the message body so
        # no page content is ever lost; page images are appended on top by
        # `_build_multimodal` when the model is vision-capable (additive, not
        # replacement). An explicit note tells the model images are attached.
        max_chars = self._configured_max_input_chars()
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"
        if pages:
            truncated += f"\n\n[Attached: {len(pages)} page image(s) of this document — also read them.]"
        user_message = f"Classify this legal document:\n\n{truncated}"

        result = self._call_structured(
            user_message,
            json_schema=schema,
            temperature=0.1,
            pages=pages,
        )
        if result.get("_parse_error"):
            logger.error("sorter_parse_error")
            return ("correspondence", 0.3, "parse error — defaulting to correspondence")
        doc_type = result.get("doc_type", "correspondence")
        confidence = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "")
        logger.info("classified", doc_type=doc_type, confidence=confidence)
        return (doc_type, confidence, reasoning)
