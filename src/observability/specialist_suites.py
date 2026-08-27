"""Dedicated scoring suite for every live mailroom specialist.

Dojo ``get_suite(doc_class)`` is the computable scorer. This module is the
mailroom registry: one suite per live extract class, mapped to the taxonomy
specialist that owns extraction. ``merger_agreement`` keeps sharing
``contracts_specialist`` as the *agent* (issue #38) but has its own MAUD
suite (rebound subclasses / extras), not the CUAD family catalog.

Schema field maps come from ``taxonomy.yaml`` ``field_types`` plus the
Pydantic extraction model. Scoring never invents Hub n=0 accuracy for
``compliance_filing`` — that class is local-pack only until Hub rows exist.
"""

from __future__ import annotations

from typing import Any

from schemas.documents import EXTRACTION_SCHEMAS

# Trace-only / non-content fields — never scored as extraction GT.
_SKIP_SCHEMA_FIELDS = frozenset({"reasoning", "confidence"})

# Live extract classes in taxonomy order. Retired court/DD are omitted.
LIVE_EXTRACT_CLASSES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "compliance_filing",
    "insurance_claim",
)

# Headline extras already in SCORE_CONFIGS / the dojo registry. Per-suite
# reports surface these when a row emitted them — they are not invented.
SUITE_HEADLINE_EXTRAS: dict[str, tuple[str, ...]] = {
    "contract": ("extraction_f1", "extraction_precision", "extraction_recall"),
    "merger_agreement": (
        "maud_question_accuracy",
        "maud_question_macro_accuracy",
        "extraction_f1",
    ),
    "corporate_record": ("extraction_f1", "entity_list_f1"),
    "correspondence": (
        "content_topic_accuracy",
        "sentiment_accuracy",
        "extraction_f1",
    ),
    "compliance_filing": ("extraction_f1", "entity_list_f1"),
    "insurance_claim": (
        "determination_consistency",
        "amount_exactness",
        "extraction_f1",
    ),
}


def _taxonomy_row(doc_class: str) -> dict[str, Any]:
    try:
        from pipeline.config import get_doc_class

        return dict(get_doc_class(doc_class) or {})
    except Exception:
        return {}


def specialist_for_class(doc_class: str | None) -> str | None:
    """Taxonomy specialist that extracts this live class (None if retired)."""
    kind = str(doc_class or "").strip()
    if not kind:
        return None
    row = _taxonomy_row(kind)
    name = row.get("specialist")
    return str(name) if name else None


def schema_fields(doc_class: str | None) -> tuple[str, ...]:
    """Ordered extraction-schema field names used as scoring labels."""
    kind = str(doc_class or "").strip()
    model = EXTRACTION_SCHEMAS.get(kind)
    if model is None:
        return ()
    return tuple(
        name for name in model.model_fields if name not in _SKIP_SCHEMA_FIELDS
    )


def field_types_for_class(doc_class: str | None) -> dict[str, str]:
    """Typed field map: taxonomy first, then the dojo suite."""
    kind = str(doc_class or "").strip()
    if not kind:
        return {}
    row = _taxonomy_row(kind)
    raw = row.get("field_types") or {}
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    try:
        from llm_dojo_scoring import get_suite

        return dict(get_suite(kind).field_types or {})
    except Exception:
        return {}


def dedicated_suite(doc_class: str | None) -> dict[str, Any]:
    """Mailroom registry row for one live extract class.

    ``suite_key`` is what ``get_suite`` expects (the doc class). ``specialist``
    is the agent that produced the extract. Merger shares the contracts
    specialist and still gets its own suite key.
    """
    kind = str(doc_class or "").strip()
    if kind not in LIVE_EXTRACT_CLASSES:
        return {}
    try:
        from llm_dojo_scoring import get_suite

        suite = get_suite(kind)
    except Exception:
        suite = None
    honesty = {}
    if suite is not None:
        honesty = {
            "suite_name": getattr(suite, "name", None),
            "in_corpus": bool(getattr(suite, "in_corpus", False)),
            "retired": bool(getattr(suite, "retired", False)),
            "honest_gap": getattr(suite, "honest_gap", None) or None,
            "subclasses": list(getattr(suite, "subclasses", None) or ()),
        }
    return {
        "doc_class": kind,
        "specialist": specialist_for_class(kind),
        "suite_key": kind,
        "schema_fields": list(schema_fields(kind)),
        "field_types": field_types_for_class(kind),
        "headline_extras": list(SUITE_HEADLINE_EXTRAS.get(kind, ())),
        **honesty,
    }


def list_dedicated_suites() -> list[dict[str, Any]]:
    """One registry row per live extract class, in taxonomy order."""
    return [dedicated_suite(kind) for kind in LIVE_EXTRACT_CLASSES]


def specialists_with_suites() -> dict[str, list[str]]:
    """Map specialist agent → live classes it scores (merger listed separately)."""
    out: dict[str, list[str]] = {}
    for kind in LIVE_EXTRACT_CLASSES:
        name = specialist_for_class(kind)
        if not name:
            continue
        out.setdefault(name, []).append(kind)
    return out


def score_dedicated_suite(
    doc_class: str,
    predicted: dict,
    expected: dict,
    *,
    doc_text: str | None = None,
) -> tuple[Any, dict[str, float]]:
    """Score one document through its dedicated specialist suite."""
    from observability.suite_scoring import score_with_suite

    return score_with_suite(
        doc_class,
        predicted,
        expected,
        field_types=field_types_for_class(doc_class),
        doc_text=doc_text,
    )


def gt_schema_coverage(doc_class: str, expected: dict | None) -> dict[str, Any]:
    """How many specialist schema fields have a scorable GT label."""
    names = schema_fields(doc_class)
    data = expected or {}
    present = [
        name for name in names
        if data.get(name) not in (None, "", [], {})
    ]
    return {
        "doc_class": doc_class,
        "specialist": specialist_for_class(doc_class),
        "n_schema": len(names),
        "n_labeled": len(present),
        "labeled_fields": present,
        "coverage": round(len(present) / len(names), 3) if names else None,
    }
