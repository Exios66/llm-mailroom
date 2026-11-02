"""Exact sandbox payload-quality policies over producer-supplied parsing facts.

Parsing, schema discovery and Pydantic validation belong to the producer.
These legacy sandbox predicates, four-decimal rates and floor taxonomy are
independent of Dojo's generic format defaults. Existing selection and truthiness
are preserved; schema_valid is a JSON-safe boolean even without typed validation.
"""
from __future__ import annotations

from typing import Any, Collection, Mapping, Sequence

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
