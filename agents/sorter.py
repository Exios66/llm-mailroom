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
2. If the document clearly matches one class, assign high confidence (0.90+).
3. If the document spans multiple categories or is ambiguous, pick the best fit and assign proportionally lower confidence.
4. If you genuinely cannot determine the type, set confidence low (below 0.50) and explain why.
5. Do NOT guess wildly — flag ambiguity instead of committing to a wrong classification.

Return a JSON object with:
- doc_type: one of the available class keys listed above
- confidence: float between 0.0 and 1.0
- reasoning: short explanation of your classification decision"""


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

    def classify(self, doc_text: str) -> tuple[str, float, str]:
        schema = build_structured_schema(
            {
                "doc_type": {"type": "string", "enum": get_all_doc_types()},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reasoning": {"type": "string"},
            }
        )
        max_chars = 12000
        truncated = doc_text[:max_chars]
        if len(doc_text) > max_chars:
            truncated += f"\n\n[... document truncated, {len(doc_text)} total chars ...]"

        result = self._call_structured(
            f"Classify this legal document:\n\n{truncated}",
            json_schema=schema,
            temperature=0.1,
        )
        if result.get("_parse_error"):
            logger.error("sorter_parse_error")
            return ("correspondence", 0.3, "parse error — defaulting to correspondence")
        doc_type = result.get("doc_type", "correspondence")
        confidence = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "")
        logger.info("classified", doc_type=doc_type, confidence=confidence)
        return (doc_type, confidence, reasoning)
