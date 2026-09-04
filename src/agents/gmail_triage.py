"""Gmail intake triage agent — free-model single-document lane (HUB-037).

The free OpenRouter triage team. Dispatched ONLY on single-document Gmail
inbox instances (one accepted attachment per email): performs the CORE steps
of the full pipeline — deterministic preparation (text read + intake
normalization, never an LLM), the triage classification read (primary doc
class, subclass when discernible, confidence, one-sentence gist, keywords),
key/concise entity extraction using the EXISTING per-class extraction schema
(HUB-048 — the triage lane returns the same key entities the paid
specialists would, so short documents like correspondence/Enron emails get a
concise entity answer free), the auditable-hash archive, and the completion
echo — WITHOUT the paid pipeline agents. Multi-document emails (2+
attachments) drop the triage approach and run the FULL paid pipeline (no
triage dispatch).

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
from schemas.documents import EXTRACTION_SCHEMAS, get_extraction_schema

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

Extraction step (HUB-048): when you classify the document, ALSO extract its key
entities into the provided `extraction` object, using the field schema given for
that document class. For SHORT documents (correspondence, emails, notices,
letters) this is the most important output: extract the sender, recipient,
communication date, and any action item / demand_amount / subject_matter the
document states — be CONCISE, never invent values, and leave a field empty/null
("") when the document does not state it. Use only the fields that belong to the
schema for the class you chose; do not output fields from a different class.

Respond with a single JSON object containing the classification keys and the
`extraction` object."""


def _triage_system_prompt() -> str:
    classes = ", ".join(get_all_doc_types()) + ", unknown"
    return TRIAGE_SYSTEM_PROMPT.replace("{classes}", classes)


# Schema-driven key-entity extraction (HUB-048): the triage lane carries the
# same per-class extraction field map the paid specialists use. The free model
# answers against it; clamping drops fields that are not in the class schema
# (so an "insurance_claim" that spuriously emits contract parties is corrected).
_EXTRACTION_FIELD_CAP = {"list[str]": 10, "str": 200, "float": None}


def extraction_schema_for(doc_class: str) -> dict | None:
    """Pydantic .model_json_schema() for the class's existing extraction model.

    Field typing is normalized so the free model sees accurate JSON types:
    Pydantic emits ``float | None`` and ``int | None`` fields under
    ``anyOf`` (or as ``"type": "string"`` with a null default) — we surface
    real ``number`` types so amount fields round-trip numerically.
    """
    model = get_extraction_schema(doc_class)
    if model is None:
        return None
    props = {}
    for name, info in (model.model_json_schema().get("properties") or {}).items():
        prop = dict(info)
        prop.pop("title", None)
        if "anyOf" in prop:
            types = {sub.get("type") for sub in prop["anyOf"] if isinstance(sub, dict)}
            if "number" in types or "integer" in types:
                prop["type"] = "number"
            else:
                prop["type"] = "string"
            prop.pop("anyOf", None)
        elif prop.get("type") == "string" and "number" in str(info.get("default", "")).lower():
            # e.g. demand_amount: float|None surfaced as string w/ null default
            if info.get("default") is None and prop.get("format") == "float":
                prop["type"] = "number"
        if "default" in info:
            prop["default"] = info["default"]
        props[name] = prop
    required = []
    for name in model.model_json_schema().get("required") or []:
        if name in props:
            required.append(name)
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _clamp_extraction(doc_class: str, raw: dict) -> dict:
    """Clamp the model's extraction object to the class's canonical schema."""
    schema = extraction_schema_for(doc_class)
    if schema is None:
        return {}
    out = {}
    props = schema.get("properties", {})
    for name, prop in props.items():
        ptype = prop.get("type", "string")
        value = raw.get(name)
        if ptype == "array":
            if not isinstance(value, list):
                continue
            items_t = prop.get("items", {}).get("type", "string")
            conv = str if items_t == "string" else (lambda x: float(x) if isinstance(x, (int, float)) else None)
            cleaned = [conv(v) for v in value[: _EXTRACTION_FIELD_CAP["list[str]"] or 10]]
            cleaned = [v for v in cleaned if v is not None and (v != "" if items_t == "string" else True)]
            if cleaned:
                out[name] = cleaned
        elif ptype == "number":
            try:
                out[name] = float(value)
            except (TypeError, ValueError):
                pass
        elif ptype == "boolean":
            if isinstance(value, bool):
                out[name] = value
            elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                out[name] = value.strip().lower() == "true"
        elif isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                out[name] = cleaned[:_EXTRACTION_FIELD_CAP["str"] or 200]
        elif value is None:
            continue
        else:  # number-as-string etc → coerce to str
            if isinstance(value, (int, float)):
                out[name] = str(value)[:_EXTRACTION_FIELD_CAP["str"] or 200]
    return out


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
    raw_extraction = result.get("extraction")
    extraction = {}
    if doc_class != "unknown" and isinstance(raw_extraction, dict):
        extraction = _clamp_extraction(doc_class, raw_extraction)
    return {
        "primary_doc_class": doc_class,
        "doc_subclass": subclass,
        "confidence": round(confidence, 3),
        "gist": str(result.get("gist") or "").strip()[:300],
        "keywords": keywords,
        "extraction": extraction,
    }


class GmailTriageAgent(BaseAgent):
    agent_name = "gmail_triage"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, TRIAGE_SYSTEM_PROMPT)
        return text.replace("{classes}", ", ".join(get_all_doc_types()) + ", unknown")

    def triage(self, doc_text: str, filename: str | None = None) -> dict:
        """Free-tier triage of one document; returns the validated result.

        The free model answers classification keys AND a per-class
        ``extraction`` object driven by the EXISTING EXTRACTION_SCHEMAS
        (HUB-048): the field map for each live doc class is injected into
        the user message so short documents (correspondence/emails) yield
        key entities (sender, recipient, date, action items, subject matter)
        without the paid specialists.
        """
        # Field map for every live class, so the free model answers the right
        # schema for whatever it classifies.
        schema_blocks = []
        for dc in get_all_doc_types():
            s = extraction_schema_for(dc)
            if s is None:
                continue
            props = ", ".join(s.get("properties", {}).keys())
            schema_blocks.append(f"- {dc}: {props or '(no fields)'}")
        fields_hint = "\n".join(schema_blocks) if schema_blocks else "(no extraction schemas)"

        user = (
            f"File: {filename or 'unnamed'}\n\n"
            f"Per-class extraction schemas (extract only the fields listed for the class you chose):\n"
            f"{fields_hint}\n\n"
            f"Document text:\n{doc_text}"
        )
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
