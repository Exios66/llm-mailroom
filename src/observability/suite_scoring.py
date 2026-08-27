"""Dedicated specialist scoring suites from llm-dojo-scoring 0.11.0.

``get_suite(doc_class)`` returns the specialist suite (merger_agreement
rebinds the MAUD catalog rather than inheriting CUAD families). Extraction
suites may wrap extras — Enron topic/sentiment on correspondence, MAUD
per-question metrics on merger agreements, insurance determination /
amount extras — beside the typed ExtractionScoreResult.

Single-doc ``suite.score(dict, dict)`` still returns ``ExtractionScoreResult``
unless content extras force a dict; field-micro P/R/F1/F2 and claims extras
are attached here so they land on the trace.

``get_suite("intake")`` is a different shape: it returns a dict (accuracy,
prep completeness, changed/messy rates, hyphen/blank counts) rather than an
``ExtractionScoreResult``. Do not force it through ``score_with_suite``.

Honesty fields on each suite (``honest_gap``, ``in_corpus``, ``retired``) are
surfaced by ``observability.honest_gaps``. SCORE_CONFIGS names must exist in
the installed dojo registry.
"""

from __future__ import annotations

from typing import Any

from llm_dojo_scoring.field_scoring import ExtractionScoreResult, score_extraction

# Score names we emit when a suite returns extras. Must exist in SCORE_CONFIGS
# and the dojo registry. ``field_presence`` is deliberately absent — dojo 0.11.0
# documents it as unemitted (honesty gap, not a scorer).
SUITE_EXTRA_SCORE_NAMES = frozenset({
    "content_topic_accuracy",
    "content_topic_f1_macro",
    "sentiment_accuracy",
    "sentiment_f1_macro",
    "maud_question_accuracy",
    "maud_question_macro_accuracy",
    "maud_clause_presence",
    "maud_valid_class_rate",
    "maud_category_accuracy",
    "extraction_precision",
    "extraction_recall",
    "extraction_f1",
    "extraction_f2",
    "entity_list_f1",
    "determination_consistency",
    "amount_exactness",
})

# Intake clerk metrics from get_suite("intake").score — dict, not extraction.
INTAKE_SCORE_NAMES = frozenset({
    "intake_prep_completeness",
    "intake_changed_rate",
    "intake_messy_rate",
    "intake_hyphen_unwraps",
    "intake_collapsed_blanks",
})


def unwrap_suite_result(out: Any) -> tuple[ExtractionScoreResult | None, dict[str, float]]:
    """Split ``suite.score`` output into the extraction result + numeric extras."""
    extras: dict[str, float] = {}
    if isinstance(out, ExtractionScoreResult):
        return out, extras
    if not isinstance(out, dict):
        return None, extras
    extraction = out.get("extraction")
    result = extraction if isinstance(extraction, ExtractionScoreResult) else None
    for key, value in out.items():
        if key in ("extraction", "detail"):
            continue
        if key not in SUITE_EXTRA_SCORE_NAMES:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        extras[key] = float(value)
    return result, extras


def _numeric_extra(name: str, value: Any) -> float | None:
    if name not in SUITE_EXTRA_SCORE_NAMES:
        return None
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def attach_single_doc_extras(
    doc_class: str,
    predicted: dict,
    expected: dict,
    result: ExtractionScoreResult,
    extras: dict[str, float],
    *,
    field_types: dict[str, str] | None = None,
) -> dict[str, float]:
    """Fill field-micro P/R/F1/F2 and insurance claims extras (dojo 0.10.0+).

    Single-doc ``suite.score`` returns ``ExtractionScoreResult`` and only
    attaches claims extras on the batch path. Mailroom always scores one
    document, so we compute those extras here.
    """
    merged = dict(extras)
    ftypes = field_types or {}
    if not ftypes:
        try:
            from llm_dojo_scoring import get_suite

            ftypes = dict(get_suite(doc_class).field_types or {})
        except Exception:
            ftypes = {}
    try:
        from llm_dojo_scoring.extraction_metrics import (
            extraction_binary_metrics,
            prf_bundle_keys,
        )

        one = extraction_binary_metrics(
            expected,
            predicted,
            field_map=ftypes,
            doc_class=doc_class,
            result=result,
        )
        for key, value in prf_bundle_keys(one).items():
            numeric = _numeric_extra(key, value)
            if numeric is not None:
                merged[key] = numeric
    except Exception:
        pass
    kind = str(doc_class or "")
    try:
        from pipeline.config import resolve_extract_class

        kind = resolve_extract_class(doc_class) or kind
    except Exception:
        pass
    if kind == "insurance_claim" or doc_class == "insurance_claim":
        try:
            from llm_dojo_scoring.claims_consistency import score_claims_extras

            for key, value in score_claims_extras(expected, predicted).items():
                numeric = _numeric_extra(key, value)
                if numeric is not None:
                    merged[key] = numeric
        except Exception:
            pass
    return merged


def score_with_suite(
    doc_class: str,
    predicted: dict,
    expected: dict,
    *,
    field_types: dict[str, str] | None = None,
    doc_text: str | None = None,
) -> tuple[ExtractionScoreResult, dict[str, float]]:
    """Score one document with the dedicated specialist suite.

    Falls back to ``score_extraction`` when ``get_suite`` has no live suite
    for the class (unknown / retired).
    """
    try:
        from llm_dojo_scoring import get_suite

        suite = get_suite(doc_class)
        out = suite.score(
            expected,
            predicted,
            doc_text=doc_text,
            field_types=field_types,
        )
        result, extras = unwrap_suite_result(out)
        if result is not None:
            extras = attach_single_doc_extras(
                doc_class,
                predicted,
                expected,
                result,
                extras,
                field_types=field_types or dict(suite.field_types or {}),
            )
            return result, extras
    except Exception:
        pass
    return score_extraction(
        doc_class, field_types or {}, predicted, expected, doc_text=doc_text
    ), {}


def score_intake_suite(
    raw_text: str,
    cleaned: str,
    stats: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Score cleaned intake output against the deterministic clerk gold.

    Returns the registry-backed numeric extras we emit on the trace. Live
    deterministic intake scored against itself is tautological for
    accuracy=1.0; completeness and the per-doc changed/messy/count flags
    are still useful, and the same path scores a future LLM intake.
    """
    extras: dict[str, float] = {}
    try:
        from llm_dojo_scoring import get_suite
        from llm_dojo_scoring.intake import INTAKE_SPAN_KEYS

        predicted: Any = cleaned
        if stats:
            payload = {k: stats[k] for k in INTAKE_SPAN_KEYS if k in stats}
            predicted = {"text": cleaned, **payload}
        out = get_suite("intake").score(raw_text, predicted)
    except Exception:
        return extras
    if not isinstance(out, dict):
        return extras
    for key in INTAKE_SCORE_NAMES:
        value = out.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        extras[key] = float(value)
    return extras


def score_and_log_intake(
    raw_text: str,
    cleaned: str,
    stats: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Attach intake-suite scores to the active trace (no-op when tracing is off)."""
    extras = score_intake_suite(raw_text, cleaned, stats)
    if not extras:
        return extras
    try:
        from observability.scores import is_enabled, score_trace

        if not is_enabled():
            return extras
        for name, value in extras.items():
            score_trace(name, value, data_type="NUMERIC")
    except Exception:
        pass
    return extras
