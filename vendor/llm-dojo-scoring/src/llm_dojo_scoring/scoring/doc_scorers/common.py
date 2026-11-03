"""Tier 0 of document scoring: what every document gets, whatever its type.

Moved from local-mailroom-sandbox ``eval/scoring.py:score_extraction_row`` (dojo
0.27.0). It scopes the pair to the live class schema, runs the class suite (the
detailed, subclass-aware path when a subclass is known or the type asks for it), and
returns the headline score, the field-micro PRF keys and ``unlabeled_fields``.
Type-specific extras and headline swaps are tiers 1 and 2, in the type modules.
"""

from __future__ import annotations

from typing import Any, Mapping

import structlog

from ..extraction_metrics import extraction_binary_metrics
from ..extraction_scope import scope_extraction_pair
from ..field_scoring import score_extraction
from ..suites import suite_for_doc_type

logger = structlog.get_logger(__name__)

#: Field-micro keys copied onto the payload when the scorer produced them.
EXTRACT_PRF_KEYS = (
    "extraction_precision",
    "extraction_recall",
    "extraction_f1",
    "extraction_f2",
    "entity_list_f1",
)

_WARNED: set[str] = set()


def _warn_once(key: str, message: str, exc: BaseException | None = None) -> None:
    """Log a scoring-path downgrade once per process and key."""
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning("doc_scorer_fallback", key=key, message=message, error=repr(exc) if exc else None)


def _overall(result: Any) -> Any:
    """Headline score from a suite result object or the suites' flat dict shape."""
    overall = getattr(result, "overall_score", None)
    if overall is None and isinstance(result, dict):
        # The suites return a flat dict whose aggregate may sit under
        # ``result["extraction"].overall_score`` (SAND-019).
        overall = result.get("overall_score")
        if overall is None:
            overall = result.get("extraction_overall_score")
        if overall is None:
            nested = result.get("extraction")
            overall = getattr(nested, "overall_score", None)
            if overall is None and isinstance(nested, dict):
                overall = nested.get("overall_score")
    return overall


def score_common(
    doc_type: str,
    predicted: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
    *,
    doc_text: str | None = None,
    doc_subclass: str | None = None,
    expected_subclass: str | None = None,
    detailed: bool | None = None,
) -> dict[str, Any]:
    """Score one document's extraction with the class suite (tier 0).

    ``doc_subclass`` wins over ``expected_subclass``. ``detailed=None`` keeps the
    sandbox rule: the detailed suite path runs when a subclass is known or the type is
    ``corporate_record``. Suite lookup or scoring errors fall back to generic
    ``score_extraction`` and say so in ``scoring_method``; a failed PRF derivation
    omits those keys. ``predicted`` and ``expected`` must be mappings (or ``None``).
    """
    sub = str(doc_subclass or expected_subclass or "").strip() or None
    scoring_method = "suite"
    try:
        suite = suite_for_doc_type(doc_type)
        field_types = getattr(suite, "field_types", None) or {}
    except Exception as exc:
        scoring_method = "generic"
        _warn_once(f"suite-unavailable-{doc_type}", "suite lookup failed; generic scoring", exc)
        field_types = {}
        suite = None
    predicted, expected = scope_extraction_pair(doc_type, predicted or {}, expected or {})
    sub_kwargs = {"doc_subclass": sub} if sub else {}
    if detailed is None:
        detailed = bool(sub or doc_type == "corporate_record")
    if suite is not None:
        try:
            if detailed:
                result = suite.score(expected, predicted, doc_text=doc_text, detailed=True, **sub_kwargs)
            else:
                result = suite.score(expected, predicted, doc_text=doc_text)
        except Exception as exc:
            scoring_method = "suite-fallback-generic"
            _warn_once(f"suite-score-failed-{doc_type}", "suite.score raised; generic scoring", exc)
            result = score_extraction(doc_type, field_types, predicted, expected, doc_text=doc_text)
    else:
        result = score_extraction(doc_type, field_types, predicted, expected, doc_text=doc_text)

    extraction_obj = result
    if isinstance(result, dict) and result.get("extraction") is not None:
        extraction_obj = result["extraction"]
    payload: dict[str, Any] = {
        "overall_extraction_score": _overall(result),
        "doc_type": doc_type,
        "scoring_method": scoring_method,
    }
    if hasattr(extraction_obj, "__dict__") and (not isinstance(result, dict) or detailed):
        payload["fields"] = {
            k: v for k, v in vars(extraction_obj).items() if k != "field_scores" and not k.startswith("_")
        }
    if hasattr(result, "__dict__"):
        try:
            prf = extraction_binary_metrics(
                expected, predicted, field_map=field_types, doc_class=doc_type, result=result, doc_text=doc_text
            )
        except Exception as exc:
            _warn_once(f"prf-failed-{doc_type}", "extraction_binary_metrics failed; PRF keys dropped", exc)
            prf = {}
        for key in EXTRACT_PRF_KEYS:
            if key in prf and prf[key] is not None:
                payload[key] = prf[key]
    elif isinstance(result, dict):
        for key in EXTRACT_PRF_KEYS:
            if key in result:
                payload[key] = result[key]
    if isinstance(result, dict) and result.get("unlabeled_fields") is not None:
        payload["unlabeled_fields"] = result["unlabeled_fields"]
    return payload
