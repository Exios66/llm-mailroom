"""Dedicated specialist scoring suites from llm-dojo-scoring 0.9.0.

``get_suite(doc_class)`` returns the specialist suite (merger_agreement
rebinds the MAUD catalog rather than inheriting CUAD families). Extraction
suites may wrap extras — Enron topic/sentiment on correspondence, MAUD
per-question metrics on merger agreements — beside the typed ExtractionScoreResult.
"""

from __future__ import annotations

from typing import Any

from llm_dojo_scoring.field_scoring import ExtractionScoreResult, score_extraction

# Score names we emit when a suite returns extras. Must exist in SCORE_CONFIGS
# and the dojo registry.
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
            return result, extras
    except Exception:
        pass
    return score_extraction(
        doc_class, field_types or {}, predicted, expected, doc_text=doc_text
    ), {}
