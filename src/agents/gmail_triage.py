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
import re
import structlog
from datetime import datetime, timezone

from agents.base import BaseAgent
from llm.prompts import get_managed_prompt
from pipeline.config import get_all_doc_types
from schemas.documents import EXTRACTION_SCHEMAS, get_extraction_schema

logger = structlog.get_logger(__name__)

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

# Best-effort correspondence-shaped fields the unknown-class fallback (HUB-049)
# and the header pass may fill — the closest shaped schema for short header-
# bearing documents when the free model cannot pin the class.
_UNKNOWN_FALLBACK_FIELDS = (
    "sender",
    "recipient",
    "additional_recipients",
    "communication_type",
    "communication_date",
    "demand_amount",
    "action_items",
    "urgency",
    "intent",
    "subject_matter",
    "keywords",
    "confidence",
)


def _deterministic_header_extraction(doc_text: str) -> dict:
    """Grounded, LLM-free best-effort header pass over short documents (HUB-049).

    Extracts sender/recipient/date/subject from a plain ``From:/To:/Date:``
    header block OR the Enron-style ``| From | value |`` markdown table.
    Only fills what is literally present — never invents values.
    """
    out: dict = {}
    line_patterns = (
        (r"^\s*From\s*:\s*(.+)$", "sender"),
        (r"^\s*To\s*:\s*(.+)$", "recipient"),
        (r"^\s*Date\s*:\s*(.+)$", "communication_date"),
        (r"^\s*Subject\s*:\s*(.+)$", "subject_matter"),
    )
    table_patterns = (
        (r"\| From \|\s*([^|\n]+)\s*\|", "sender"),
        (r"\| To \|\s*([^|\n]+)\s*\|", "recipient"),
        (r"\| Date \|\s*([^|\n]+)\s*\|", "communication_date"),
        (r"\| Subject \|\s*([^|\n]+)\s*\|", "subject_matter"),
    )
    import re

    flags = re.IGNORECASE | re.MULTILINE
    for pattern, key in line_patterns + table_patterns:
        m = re.search(pattern, doc_text, flags)
        value = m.group(1).strip() if m else ""
        value = re.sub(r"\s+", " ", value).strip().strip("|").strip()
        if value and key not in out:
            out[key] = value[:_EXTRACTION_FIELD_CAP["str"] or 200]
    return out


def _unknown_class_extraction(raw_extraction: dict, doc_text: str) -> dict:
    """HUB-049 fallback: when the class is unknown, still return grounded key
    entities for short header-bearing documents.

    Combines (a) any correspondence-shaped keys the model provided and (b) a
    deterministic header pass over the literal text (never-invent law).
    """
    import re

    from schemas.documents import CorrespondenceExtraction

    corr_props = set((CorrespondenceExtraction.model_json_schema().get("properties") or {}).keys())
    merged: dict = {}
    if isinstance(raw_extraction, dict):
        for key in list(raw_extraction.keys()):
            if key in corr_props and key in _UNKNOWN_FALLBACK_FIELDS:
                value = raw_extraction[key]
                if key in ("additional_recipients", "action_items", "keywords"):
                    if isinstance(value, list):
                        merged[key] = [str(v).strip()[:80] for v in value[:10] if str(v).strip()]
                elif isinstance(value, (str, int, float, bool)) and not (
                    isinstance(value, str) and not value.strip()
                ):
                    merged[key] = value
    for key, value in _deterministic_header_extraction(doc_text).items():
        merged.setdefault(key, value)
    return merged


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


def validate_triage(result: dict, doc_text: str | None = None) -> dict:
    """Clamp the free model's answer to the live taxonomy vocabulary.

    When ``doc_text`` is provided and the class clamps to ``unknown``, the
    HUB-049 best-effort fallback fills grounded key entities (sender/
    recipient/date/subject from a literal header pass + any model-provided
    correspondence-shaped keys) so a short document that the free model
    could not pin still yields a concise entity answer. A non-unknown class
    with a raw extraction is clamped to that class's canonical schema.
    """
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
    if doc_class == "unknown":
        extraction = _unknown_class_extraction(
            raw_extraction if isinstance(raw_extraction, dict) else {}, doc_text or ""
        )
    elif isinstance(raw_extraction, dict):
        extraction = _clamp_extraction(doc_class, raw_extraction)
    return {
        "primary_doc_class": doc_class,
        "doc_subclass": subclass,
        "confidence": round(confidence, 3),
        "gist": str(result.get("gist") or "").strip()[:300],
        "keywords": keywords,
        "extraction": extraction,
    }


def _parse_model_json(raw: str) -> tuple[dict | None, str | None]:
    """Parse a free model's JSON answer, tolerating the mess they emit.

    The OpenRouter free tier answers through different upstreams with
    different output habits: bare JSON, markdown-fenced JSON (```json ...```),
    prose with a JSON object embedded, or occasionally a non-JSON apology.
    Returns ``(parsed, None)`` on success, ``(None, reason)`` when nothing
    parseable exists — the caller parks the document with the evidence.
    """
    if raw is None:
        return None, "empty response (None)"
    text = str(raw).strip()
    if not text:
        return None, "empty response"
    # 1) direct parse
    try:
        obj = json.loads(text)
        return (obj if isinstance(obj, dict) else None), (
            None if isinstance(obj, dict) else "response was not a json object"
        )
    except json.JSONDecodeError:
        pass
    # 2) markdown code fences (```json ... ``` or ``` ... ```)
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            return (obj if isinstance(obj, dict) else None), (
                None if isinstance(obj, dict) else "fenced response was not a json object"
            )
        except json.JSONDecodeError:
            pass
    # 3) first '{' to last '}' — prose-wrapped objects
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return (obj if isinstance(obj, dict) else None), (
                None if isinstance(obj, dict) else "embedded response was not a json object"
            )
        except json.JSONDecodeError:
            pass
    return None, "no parseable json object in response"


def _write_triage_debug(
    *,
    filename: str | None,
    system: str,
    user: str,
    response: str,
    result: dict,
    extra: dict | None = None,
) -> str | None:
    """Write the FULL triage I/O payloads for debugging (human directive).

    Everything lands under ``<base>/debug/triage/<UTC stamp>_<file slug>/``:
    ``system.txt``, ``user.txt``, ``response.txt``, ``result.json`` and a
    ``meta.json`` summary. Best-effort — a debug-write failure must never
    affect the triage lane.
    """
    try:
        from pipeline.bins import get_base_dir

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(filename or "unnamed"))[:60]
        d = get_base_dir() / "debug" / "triage" / f"{stamp}_{slug}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "system.txt").write_text(system, encoding="utf-8")
        (d / "user.txt").write_text(user, encoding="utf-8")
        (d / "response.txt").write_text(response, encoding="utf-8")
        (d / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "written_at": datetime.now(timezone.utc).isoformat(),
                    "filename": filename,
                    "input_chars": len(user),
                    "response_chars": len(response),
                    **(extra or {}),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return str(d)
    except Exception:  # noqa: BLE001 — debugging aids never break the lane
        logger.exception("triage_debug_write_failed")
        return None


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

        FULL I/O DEBUG CAPTURE (human directive 2026-09-04): every call
        writes the complete system prompt, user message, raw model response,
        and validated result under ``<base>/debug/triage/<stamp>_<file>/`` —
        free-tier models fail in messy ways (fenced JSON, prose-wrapped
        objects, truncated output), and debugging them REQUIRES the full
        payloads, not excerpts.
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

        # Capture the EXACT final prompt/response by wrapping the transport
        # call: _call_structured appends the json_object boilerplate + schema
        # to the user message, so only the transport sees the full payload.
        captured: dict = {}
        original_call_llm = self._call_llm

        def _capture_call_llm(user_message: str, **kwargs):
            captured["user"] = user_message
            captured["system"] = kwargs.get("system_prompt")
            raw = original_call_llm(user_message, **kwargs)
            captured["raw"] = raw
            return raw

        self._call_llm = _capture_call_llm  # type: ignore[method-assign]
        debug_dir: str | None = None
        parse_error: str | None = None
        try:
            raw = self._call_structured(
                user,
                TRIAGE_SCHEMA,
                system_prompt=self.system_prompt(),
            )
        except Exception as exc:
            # The call itself failed (rate-limit exhaustion, timeout, …).
            # Park evidence first: the full INPUT is written even though no
            # response exists, then re-raise so the lane parks the document
            # with the real reason.
            debug_dir = _write_triage_debug(
                filename=filename,
                system=str(captured.get("system") or ""),
                user=str(captured.get("user") or ""),
                response=str(captured.get("raw") or ""),
                result={"error": f"{type(exc).__name__}: {exc}"},
                extra={"exception": type(exc).__name__},
            )
            logger.warning(
                "triage_llm_call_failed",
                agent=self.agent_name,
                model=getattr(self, "model", None),
                error_type=type(exc).__name__,
                detail=str(exc)[:300],
                debug_dir=debug_dir,
            )
            raise
        finally:
            del self._call_llm  # type: ignore[attr-defined]

        try:
            if isinstance(raw, str):
                parsed, parse_error = _parse_model_json(raw)
            elif isinstance(raw, dict) and raw.get("_parse_error"):
                # _call_structured's own parse failed — recover its raw text.
                parsed, parse_error = _parse_model_json(str(raw.get("_raw") or ""))
            else:
                parsed, parse_error = (raw if isinstance(raw, dict) else None), (
                    None if isinstance(raw, dict) else f"non-dict response: {type(raw).__name__}"
                )
        except Exception as exc:  # noqa: BLE001 — parse must never raise out
            parsed, parse_error = None, f"{type(exc).__name__}: {exc}"

        if parsed is None:
            logger.warning(
                "triage_parse_failed",
                agent=self.agent_name,
                model=getattr(self, "model", None),
                parse_error=parse_error,
                raw_head=str(captured.get("raw"))[:300],
                raw_tail=str(captured.get("raw"))[-300:],
            )

        result = validate_triage(parsed if isinstance(parsed, dict) else {}, doc_text=doc_text)

        # Fail-soft with evidence: an unparseable free-model response parks
        # the document (unknown/low-confidence gate downstream) but the
        # manifest and echo must carry WHY, plus where the full payloads are.
        response_text = str(captured.get("raw") or "")
        debug_block = {
            "model": getattr(self, "model", None),
            "parse_ok": parsed is not None,
            "parse_error": parse_error,
            "input_chars": len(str(captured.get("user") or "")),
            "response_chars": len(response_text),
        }
        result["debug"] = debug_block

        debug_dir = _write_triage_debug(
            filename=filename,
            system=str(captured.get("system") or ""),
            user=str(captured.get("user") or ""),
            response=response_text,
            result=result,
            extra={"parse_error": parse_error} if parse_error else None,
        )
        if debug_dir:
            debug_block["debug_dir"] = debug_dir
            if parsed is None:
                result["debug"]["debug_dir"] = debug_dir
        logger.info(
            "triage_llm_io",
            agent=self.agent_name,
            model=debug_block["model"],
            input_chars=debug_block["input_chars"],
            response_chars=debug_block["response_chars"],
            parse_ok=debug_block["parse_ok"],
            doc_class=result.get("primary_doc_class"),
            confidence=result.get("confidence"),
            debug_dir=debug_dir,
        )
        return result
