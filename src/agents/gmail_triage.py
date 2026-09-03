"""Gmail intake triage agent — free-model single-document lane (HUB-037).

The free OpenRouter triage team. Dispatched ONLY on single-document Gmail
inbox instances (one accepted attachment per email): performs the CORE steps
of the full pipeline — deterministic preparation (text read + intake
normalization, never an LLM), the triage classification read (primary doc
class, subclass when discernible, confidence, one-sentence gist, keywords),
the auditable-hash archive, and the completion echo — WITHOUT the paid
pipeline agents. Multi-document emails (2+ attachments) drop the triage
approach and run the FULL paid pipeline (no triage dispatch).

Advisory by design: the triage result never overrides the pipeline's own
classification — it rides `intake_meta["triage"]` into the manifest and the
audit chain (`triage_*` event section, never conflated with the pipeline's
`ingested/classified/extracted/archived` vocabulary). Fails soft: a triage
failure must never block the intake.
"""

from __future__ import annotations

import json

from agents.base import BaseAgent
from llm.prompts import get_managed_prompt
from pipeline.config import get_all_doc_types

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_doc_class": {"type": "string"},
        "doc_subclass": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "gist": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["primary_doc_class", "confidence", "gist"],
}

TRIAGE_SYSTEM_PROMPT = """You are the intake triage clerk of a legal mailroom. A document has arrived by email. Your job is a FAST, GROUNDED first read — the full pipeline will re-classify and extract later; your read is the accurate intake log shown to the human.

Classify the document into ONE of these primary classes:
{classes}
Use "unknown" when the document does not clearly fit any of them — never guess.

If the class has a well-known subtype catalog and the document clearly matches one, set doc_subclass to that token; otherwise null.

Rules:
- Ground everything in the document text. Do not invent parties, dates, or amounts.
- confidence is 0.0-1.0 and must reflect how certain the primary class is.
- gist: ONE grounded sentence saying what this document actually is.
- keywords: at most 6 distinctive terms from the document.
Respond with a single JSON object only."""


def _triage_system_prompt() -> str:
    classes = ", ".join(get_all_doc_types()) + ", unknown"
    return TRIAGE_SYSTEM_PROMPT.replace("{classes}", classes)


def validate_triage(result: dict) -> dict:
    """Clamp the free model's answer to the live taxonomy vocabulary."""
    live = set(get_all_doc_types())
    doc_class = str(result.get("primary_doc_class") or "unknown").strip().lower()
    if doc_class not in live:
        doc_class = "unknown"
    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    subclass = result.get("doc_subclass") or None
    if subclass is not None:
        subclass = str(subclass).strip()[:80] or None
    raw_kw = result.get("keywords")
    if not isinstance(raw_kw, list):
        raw_kw = []
    keywords = [str(k).strip()[:80] for k in raw_kw[:6] if str(k).strip()]
    return {
        "primary_doc_class": doc_class,
        "doc_subclass": subclass,
        "confidence": round(confidence, 3),
        "gist": str(result.get("gist") or "").strip()[:300],
        "keywords": keywords,
    }


class GmailTriageAgent(BaseAgent):
    agent_name = "gmail_triage"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, TRIAGE_SYSTEM_PROMPT)
        return text.replace("{classes}", ", ".join(get_all_doc_types()) + ", unknown")

    def triage(self, doc_text: str, filename: str | None = None) -> dict:
        """Free-tier triage of one document; returns the validated result."""
        user = f"File: {filename or 'unnamed'}\n\nDocument text:\n{doc_text}"
        raw = self._call_structured(
            user,
            TRIAGE_SCHEMA,
            system_prompt=self.system_prompt(),
        )
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        return validate_triage(raw if isinstance(raw, dict) else {})
