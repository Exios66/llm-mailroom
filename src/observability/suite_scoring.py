"""Dedicated specialist scoring suites from llm-dojo-scoring (0.19.1 pin).

``get_suite(doc_class)`` returns the specialist suite (merger_agreement
rebinds the MAUD catalog rather than inheriting CUAD families). Extraction
suites may wrap extras — Enron topic/sentiment on correspondence, MAUD
per-question metrics on merger agreements, insurance determination /
amount extras — beside the typed ExtractionScoreResult.

Mailroom scores one document at a time through ``suite.score_document``
(dojo 0.19): one payload with the extraction result, field-micro
P/R/F1/F2, class extras, the format layer, ``metric_id`` and provenance.
Unscorable GT fails closed instead of producing a phantom score.

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
# and the dojo registry. ``field_presence`` is deliberately absent — dojo 0.19.1
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


def is_unscorable(payload: Any) -> bool:
    """True when dojo failed the document closed (``status == "unscorable"``)."""
    return isinstance(payload, dict) and payload.get("status") == "unscorable"


def score_document_payload(
    doc_class: str,
    predicted: dict,
    expected: dict,
    *,
    field_types: dict[str, str] | None = None,
    doc_text: str | None = None,
    **provenance: Any,
) -> dict[str, Any] | None:
    """Full per-document scorecard from ``suite.score_document`` (dojo 0.19+).

    One call carries the ``ExtractionScoreResult`` (``extraction``),
    field-micro P/R/F1/F2, single-document class extras (content / MAUD /
    insurance consistency), the format layer (``parse_ok`` /
    ``schema_valid`` / ``schema_adherence`` — a fraction, unlike the
    pipeline's boolean ``schema_valid`` trace score), ``metric_id`` and a
    ``provenance`` block. Hub ``gt_fields`` metadata is parsed and scoped to
    the class inside dojo. GT with nothing scorable comes back as
    ``{"status": "unscorable", "reason": ...}`` — fail-closed, never a
    phantom score.

    ``provenance`` kwargs (``dataset_revision``, ``split``, ``draw_seed``,
    ``prompt_id``, ``model_id``, ``serving_kind``) are stamped on the
    payload. Returns ``None`` when no live extraction suite exists for the
    class or dojo raises.
    """
    try:
        from llm_dojo_scoring import get_suite

        suite = get_suite(doc_class)
        stamps = {k: v for k, v in provenance.items() if v is not None}
        return suite.score_document(
            expected,
            predicted,
            doc_text=doc_text,
            field_types=field_types,
            **stamps,
        )
    except Exception:
        return None


def payload_extras(payload: dict[str, Any] | None) -> dict[str, float]:
    """Registry-backed numeric extras from a ``score_document`` payload."""
    extras: dict[str, float] = {}
    for key, value in (payload or {}).items():
        numeric = _numeric_extra(key, value)
        if numeric is not None:
            extras[key] = numeric
    return extras


def unscorable_result(doc_class: str) -> ExtractionScoreResult:
    """Empty result for fail-closed GT: no field scores, ``overall_score=None``."""
    return ExtractionScoreResult(
        doc_class=doc_class, field_scores={}, overall_score=None, ambiguous_fields=[]
    )


def score_with_suite(
    doc_class: str,
    predicted: dict,
    expected: dict,
    *,
    field_types: dict[str, str] | None = None,
    doc_text: str | None = None,
) -> tuple[ExtractionScoreResult, dict[str, float]]:
    """Score one document with the dedicated specialist suite.

    Routes through ``suite.score_document`` so field-micro P/R/F1/F2 and the
    insurance single-document extras come from dojo, not a local re-derivation.
    Unscorable GT returns an empty result (``overall_score=None``) with no
    extras. Falls back to ``score_extraction`` only when ``get_suite`` has no
    live extraction suite for the class (unknown / retired).
    """
    payload = score_document_payload(
        doc_class, predicted, expected, field_types=field_types, doc_text=doc_text
    )
    if is_unscorable(payload):
        return unscorable_result(doc_class), {}
    if payload is not None:
        result = payload.get("extraction")
        if isinstance(result, ExtractionScoreResult):
            return result, payload_extras(payload)
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
