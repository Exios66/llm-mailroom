"""Sandbox payload-quality policies: parsing, schema adherence and floor modes.

``assess_extraction_payload`` (moved from local-mailroom-sandbox for the unified
audit row, dojo 0.27.0) turns a specialist prediction into a dict when it can and
classifies parse vs schema problems. Schema discovery and Pydantic validation stay
with the producer: pass ``schema_lookup`` (mailroom's ``get_extraction_schema``)
and the dojo never imports mailroom. Without it, the structural fallback and the
built-in key lists apply. ``assess_extraction_facts`` applies the same rubric to
facts a producer already computed. Legacy sandbox predicates, four-decimal rates
and the floor taxonomy are kept as they were.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Callable, Collection, Mapping, Sequence

#: ``doc_type -> pydantic model class or None`` (mailroom's ``get_extraction_schema``).
SchemaLookup = Callable[[str], Any]

# Keys the specialist / scorer may attach that are not extraction fields.
_META_KEYS = frozenset(
    {
        "_parse_error",
        "_raw",
        "confidence",
        "reasoning",
        "offline_fallback",
        "error",
    }
)

# Retired correspondence schema (production-doctrine still names these).
_LEGACY_CORRESPONDENCE_KEYS = frozenset({"key_points", "referenced_communications"})

# Hub GT provenance / content differentiators — not CorrespondenceExtraction.
_NON_EXTRACTION_GT_KEYS = frozenset(
    {
        "content_topic",
        "topic_evidence",
        "sentiment_label",
        "sentiment_score",
        "sentiment_evidence",
        "label_evidence",
        "intent_source",
        "intent_confidence",
        "intent_status",
        "claimed_amount",  # insurance schema; correspondence uses demand_amount
    }
)


_FALLBACK_SCHEMA_KEYS: dict[str, frozenset[str]] = {
    "correspondence": frozenset(
        {
            "sender", "recipient", "additional_recipients", "communication_type",
            "communication_date", "demand_amount", "action_items", "urgency",
            "intent", "subject_matter", "keywords",
        }
    ),
    "contract": frozenset(
        {
            "document_name", "parties", "effective_date", "term_length", "governing_law",
            "contract_value", "renewal_terms", "cuad_family", "merger_consideration",
            "cuad_clauses", "maud_clauses",
        }
    ),
    "insurance_claim": frozenset(
        {
            "claim_number", "policy_number", "insurer", "insured_party", "claim_type",
            "date_of_loss", "date_filed", "claimed_amount", "adjuster",
            "damages_description", "coverage_determination", "denial_reasons",
            "supporting_documents", "intent", "subject_matter", "keywords",
            "claim_checklist",
        }
    ),
    "corporate_record": frozenset(
        {
            "entity_name", "record_type", "effective_date", "signatories", "jurisdiction",
            "filing_number", "intent", "subject_matter", "keywords",
        }
    ),
    "merger_agreement": frozenset(
        {
            "document_name", "parties", "effective_date", "effective_time",
            "governing_law", "merger_consideration", "maud_clauses", "intent",
            "subject_matter", "keywords",
        }
    ),
}


_JSON_OBJECT_START = re.compile(r'\{\s*["}]')

_MODEL_CLASSES = ("correspondence", "contract", "insurance_claim", "corporate_record", "merger_agreement")


def schema_keys_for(
    doc_type: str, doc_subclass: str | None = None, *, schema_lookup: SchemaLookup | None = None
) -> frozenset[str]:
    """Return live extraction field names, or an empty set for an unknown type.

    A corporate ``doc_subclass`` adds its registered extension fields to the core
    keys. Other types use the producer's model (``schema_lookup``) when it has one,
    then the built-in fallback keys, including matches for agent names.
    """
    resolved = str(doc_type or "").strip()
    if resolved == "corporate_record" and doc_subclass:
        from ..schemas.corporate_record_schema import entity_field_types

        return _FALLBACK_SCHEMA_KEYS[resolved] | frozenset(entity_field_types(doc_subclass))
    if schema_lookup is not None:
        try:
            model = schema_lookup(resolved)
            if model is not None:
                return frozenset(model.model_fields) - {"confidence", "reasoning"}
        except Exception:
            pass
    if resolved in _FALLBACK_SCHEMA_KEYS:
        return _FALLBACK_SCHEMA_KEYS[resolved]
    for suffix, keys in _FALLBACK_SCHEMA_KEYS.items():
        if resolved.endswith(suffix) or suffix in resolved:
            return keys
    return frozenset()


def coerce_predicted_payload(predicted: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Turn a specialist prediction into a dict, or explain why it is not one.

    Isolated evals keep a dict. Job ``items.jsonl`` stores ``str(value)`` (Python
    repr, not JSON). Raw LLM text may wrap a JSON object in prose.
    """
    meta: dict[str, Any] = {"parse_error": False, "parse_failure_reason": None, "prose_wrapper": False}
    if predicted is None:
        meta["parse_error"] = True
        meta["parse_failure_reason"] = "null_payload"
        return None, meta
    if isinstance(predicted, Mapping):
        # Any mapping (MappingProxyType, UserDict, ...) is a dict payload; score_document
        # accepts them, so adherence must too.
        predicted = dict(predicted)
        if predicted.get("_parse_error"):
            meta["parse_error"] = True
            meta["parse_failure_reason"] = "flagged_parse_error"
            raw = predicted.get("_raw")
            if isinstance(raw, str) and raw.strip():
                recovered, raw_meta = coerce_predicted_payload(raw)
                if recovered is not None and not raw_meta.get("parse_error"):
                    # Still a parse_error (the live path dropped the fields); keep the
                    # recovered dict for schema checks.
                    meta["recovered_from_raw"] = True
                    return recovered, meta
        return predicted, meta
    if isinstance(predicted, (bytes, bytearray)):
        try:
            predicted = predicted.decode("utf-8")
        except Exception:
            meta["parse_error"] = True
            meta["parse_failure_reason"] = "undecodable_bytes"
            return None, meta
    if not isinstance(predicted, str):
        meta["parse_error"] = True
        meta["parse_failure_reason"] = f"unsupported_type:{type(predicted).__name__}"
        return None, meta
    text = predicted.strip()
    if not text:
        meta["parse_error"] = True
        meta["parse_failure_reason"] = "empty_string"
        return None, meta
    parsed, how = _parse_object_text(text)
    if parsed is not None:
        if how in ("wrapped_json", "wrapped_python_repr"):
            meta["prose_wrapper"] = True
        return parsed, meta
    meta["parse_error"] = True
    meta["parse_failure_reason"] = "unparseable"
    return None, meta


def _parse_object_text(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse JSON, a Python repr, or either wrapped in prose; say which it was."""
    # RecursionError: deeply nested input ("[[[[...") exhausts the decoder's stack;
    # that is unparseable text, not a crash (score_document never raises).
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, "json"
    except (json.JSONDecodeError, RecursionError):
        pass
    try:
        obj = ast.literal_eval(text)
        if isinstance(obj, dict):
            return obj, "python_repr"
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        pass
    # First "{" to last "}" (what the greedy r"\{.*\}" matched), found without the regex:
    # on text with many "{" and no "}" the regex rescans from every "{" (quadratic).
    lo, hi = text.find("{"), text.rfind("}")
    span = text[lo : hi + 1] if lo != -1 and hi > lo else None
    if span is not None and (text[:lo].strip() or text[hi + 1 :].strip()):
        try:
            obj = json.loads(span)
            if isinstance(obj, dict):
                return obj, "wrapped_json"
        except (json.JSONDecodeError, RecursionError):
            pass
        try:
            obj = ast.literal_eval(span)
            if isinstance(obj, dict):
                return obj, "wrapped_python_repr"
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
            pass
    # The greedy span runs from the first "{" to the last "}", so braces in the surrounding
    # prose break it. Fall back to decoding a JSON object at each candidate "{" in turn.
    # Only `{"` / `{}` can open a JSON object; skipping other braces keeps this linear
    # (each failed raw_decode builds a JSONDecodeError that scans up to its position).
    decoder = json.JSONDecoder()
    candidate = _JSON_OBJECT_START.search(text)
    while candidate is not None:
        resume = candidate.start() + 1
        try:
            obj, _end = decoder.raw_decode(text, candidate.start())
        except json.JSONDecodeError:
            obj = None
        except RecursionError:
            # Nesting too deep to decode. Every "{" inside this object is deeper still,
            # so skip past its closing brace (found by one linear scan) and keep looking
            # for a sibling object; retrying the inner braces would be quadratic.
            resume = _balanced_object_end(text, candidate.start())
            if resume < 0:
                return None, None  # never closes: every later "{" is inside it
            obj = None
        if isinstance(obj, dict):
            return obj, "wrapped_json"
        candidate = _JSON_OBJECT_START.search(text, resume)
    return None, None


def _balanced_object_end(text: str, start: int) -> int:
    """Index just past the "}" that closes the "{" at ``start``, or -1 if it never closes.

    Counts braces outside JSON strings (with backslash escapes); used only to skip an
    object too deeply nested to decode, so one pass over it keeps the scan linear.
    """
    depth = 0
    in_string = escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _typed_valid(
    doc_type: str,
    payload: Mapping[str, Any],
    doc_subclass: str | None,
    schema_lookup: SchemaLookup | None,
) -> bool | None:
    """Validate core fields and any corporate subclass extensions.

    Corporate extension values must be string lists or nullable strings by their
    registered type. Returns ``None`` when no core model is available.
    """
    if str(doc_type or "").strip() == "corporate_record" and doc_subclass:
        from ..schemas.corporate_record_schema import entity_field_types

        for key, field_type in entity_field_types(doc_subclass).items():
            if key not in payload:
                continue
            value = payload[key]
            if field_type.startswith("entity_list"):
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    return False
            elif value is not None and not isinstance(value, str):
                return False
    if schema_lookup is None:
        return None
    try:
        model = schema_lookup(str(doc_type or "").strip())
        if model is None:
            # Agent name like correspondence_specialist.
            for cls in _MODEL_CLASSES:
                if cls in str(doc_type):
                    model = schema_lookup(cls)
                    break
    except Exception:
        return None
    if model is None:
        return None
    try:
        model.model_validate(dict(payload))
        return True
    except Exception:
        return False


def assess_extraction_payload(
    predicted: Any,
    doc_type: str,
    doc_subclass: str | None = None,
    *,
    schema_lookup: SchemaLookup | None = None,
) -> dict[str, Any]:
    """Classify format/parse vs schema for one specialist prediction.

    Returns additive keys only. ``schema_adherence`` is 1.0 when the payload parsed
    *and* validated, else 0.0. Extra and legacy keys are reported even when pydantic
    would ignore them. ``doc_subclass`` selects corporate extension keys and value
    types. Without a core validator, a nonempty parsed payload passes when the schema
    is unknown, no public keys are extra, or at least one public key is known.
    """
    payload, meta = coerce_predicted_payload(predicted)
    parse_error = bool(meta.get("parse_error"))
    extra: list[str] = []
    legacy: list[str] = []
    schema_valid = False
    if payload is not None and not parse_error:
        keys = schema_keys_for(doc_type, doc_subclass, schema_lookup=schema_lookup)
        public = [k for k in payload if k not in _META_KEYS]
        extra = sorted(k for k in public if keys and k not in keys)
        legacy = sorted(k for k in public if k in _LEGACY_CORRESPONDENCE_KEYS)
        typed = _typed_valid(doc_type, payload, doc_subclass, schema_lookup)
        if typed is None:
            schema_valid = bool(payload) and bool(not keys or not extra or set(public) & keys)
        else:
            schema_valid = bool(typed)
    adherence = (not parse_error) and schema_valid
    out: dict[str, Any] = {
        "parse_error": parse_error,
        "schema_valid": schema_valid,
        "schema_adherence": 1.0 if adherence else 0.0,
        "parse_failure_reason": meta.get("parse_failure_reason"),
        "extra_schema_keys": extra,
        "extra_schema_key_count": len(extra),
        "legacy_schema_keys": legacy,
        "prose_wrapper": bool(meta.get("prose_wrapper")),
    }
    if meta.get("recovered_from_raw"):
        out["recovered_from_raw"] = True
    return out


def assess_extraction_facts(
    payload: Mapping[str, Any] | None,
    meta: Mapping[str, Any],
    *,
    schema_keys: Collection[str] = (),
    typed_valid: bool | None = None,
) -> dict[str, Any]:
    """Apply the sandbox adherence rubric to parsed payload and validation facts."""
    parse_error = bool(meta.get("parse_error"))
    extra: list[str] = []
    legacy: list[str] = []
    schema_valid = False
    if payload is not None and not parse_error:
        keys = frozenset(schema_keys)
        public = [k for k in payload if k not in _META_KEYS]
        extra = sorted(k for k in public if keys and k not in keys)
        legacy = sorted(k for k in public if k in _LEGACY_CORRESPONDENCE_KEYS)
        typed = typed_valid
        if typed is None:
            # Structural fallback: a dict whose public keys are a subset of the
            # known schema (or unknown doc type → any dict that parsed).
            schema_valid = bool(public) and bool(not keys or not extra or set(public) & keys)
        else:
            schema_valid = bool(typed)
    elif payload is not None and parse_error:
        # Flagged parse_error: never schema_valid, even if a dict remains.
        schema_valid = False

    adherence = (not parse_error) and schema_valid
    out: dict[str, Any] = {
        "parse_error": parse_error,
        "schema_valid": schema_valid,
        "schema_adherence": 1.0 if adherence else 0.0,
        "parse_failure_reason": meta.get("parse_failure_reason"),
        "extra_schema_keys": extra,
        "extra_schema_key_count": len(extra),
        "legacy_schema_keys": legacy,
        "prose_wrapper": bool(meta.get("prose_wrapper")),
    }
    if meta.get("recovered_from_raw"):
        out["recovered_from_raw"] = True
    return out


def merge_schema_adherence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Run-level rates from per-row ``assess_extraction_payload`` dicts.

    Additive names only (``*_rate`` / ``*_count``). Does not emit ``n``.
    """
    n = len(rows)
    if n == 0:
        return {}
    parse_n = sum(1 for r in rows if r.get("parse_error"))
    valid_n = sum(1 for r in rows if r.get("schema_valid"))
    adhere_n = sum(
        1
        for r in rows
        if (not r.get("parse_error")) and r.get("schema_valid")
    )
    wrapper_n = sum(1 for r in rows if r.get("prose_wrapper"))
    extra_n = sum(1 for r in rows if int(r.get("extra_schema_key_count") or 0) > 0)
    legacy_n = sum(1 for r in rows if r.get("legacy_schema_keys"))
    return {
        "parse_error_count": parse_n,
        "parse_error_rate": round(parse_n / n, 4),
        "schema_valid_count": valid_n,
        "schema_valid_rate": round(valid_n / n, 4),
        "schema_adherence_rate": round(adhere_n / n, 4),
        "prose_wrapper_count": wrapper_n,
        "extra_schema_key_docs": extra_n,
        "legacy_schema_key_docs": legacy_n,
    }


def classify_floor_mode(row: Mapping[str, Any]) -> str:
    """Bucket a low-scoring row into issue #21's (a)/(b)/(c) modes.

    * ``format`` — parse failure or prose wrapper (a)
    * ``schema`` — parsed but extra/legacy keys the scorer does not credit (a/b)
    * ``scorer`` — schema-clean payload; quality zero is a value miss (c)
    """
    if row.get("parse_error") or row.get("prose_wrapper"):
        return "format"
    extra = set(row.get("extra_schema_keys") or [])
    legacy = set(row.get("legacy_schema_keys") or [])
    if extra or legacy:
        if extra & _NON_EXTRACTION_GT_KEYS or extra & _LEGACY_CORRESPONDENCE_KEYS:
            return "schema"
        return "schema"
    return "scorer"
