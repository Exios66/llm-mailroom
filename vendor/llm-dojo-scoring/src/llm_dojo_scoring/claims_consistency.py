"""Insurance determination-consistency and amount-exactness scorers.

These are **computed** cross-field / typed-exact metrics — not invented KPIs.
``determination_consistency`` checks that ``coverage_determination`` agrees
with ``denial_reasons``:

* ``approved`` ⇒ empty denial reasons
* ``denied`` / ``partial`` ⇒ non-empty reasons
* missing determination ⇒ 0.0

``amount_exactness`` is the complement of ``money_mae_usd``: 1.0 when the
money field matches after the existing one-cent normalize, else 0.0.
"""

from __future__ import annotations

from typing import Any, Mapping

from .field_scoring import parse_money, score_money_field

_EMPTY = (None, "", [], {})
_APPROVED = {"approved", "approve", "covered", "yes"}
_DENIED = {"denied", "deny", "rejected", "not_covered", "uncovered"}
_PARTIAL = {"partial", "partially_approved", "partially_denied"}


def _norm(value: Any) -> str:
    """Lowercase a label and replace spaces and hyphens with underscores."""
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _is_empty(value: Any) -> bool:
    """Recognize empty markers, blank strings, and recursively empty sequences."""
    if value in _EMPTY:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple)):
        return not [item for item in value if not _is_empty(item)]
    return False


def determination_consistency(
    expected: Mapping[str, Any] | None = None,
    predicted: Mapping[str, Any] | None = None,
    *,
    determination_field: str = "coverage_determination",
    reasons_field: str = "denial_reasons",
) -> float:
    """Score a single predicted extraction for determination/reason coherence.

    Ground truth is unused: this is a structural check on the prediction.
    Returns 1.0 when the pair is consistent, 0.0 otherwise.
    """
    del expected  # structural check on predicted only
    predicted = predicted or {}
    determination = _norm(predicted.get(determination_field))
    reasons = predicted.get(reasons_field)
    empty_reasons = _is_empty(reasons)
    if not determination:
        return 0.0
    if determination in _APPROVED:
        return 1.0 if empty_reasons else 0.0
    if determination in _DENIED or determination in _PARTIAL:
        return 1.0 if not empty_reasons else 0.0
    # Unknown determination token still requires the field to be present;
    # treat as consistent only if reasons are empty (no invented denial).
    return 1.0 if empty_reasons else 0.0


def amount_exactness(
    expected: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    *,
    field_name: str = "claimed_amount",
) -> float | None:
    """1.0 if the money field matches after normalize; 0.0 if both parseable
    and mismatch; ``None`` if either side is empty / unparseable."""
    expected = expected or {}
    predicted = predicted or {}
    exp = expected.get(field_name)
    pred = predicted.get(field_name)
    if _is_empty(exp) or _is_empty(pred):
        return None
    if parse_money(exp) is None or parse_money(pred) is None:
        return None
    # score_money_field(pred, exp) — 1.0 at one-cent tolerance, else 0.0.
    return float(score_money_field(pred, exp))


def score_claims_extras(
    expected: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
) -> dict[str, float | None]:
    """Batch-friendly extras for ``insurance_claims_specialist``."""
    return {
        "determination_consistency": determination_consistency(expected, predicted),
        "amount_exactness": amount_exactness(expected, predicted),
    }


__all__ = [
    "amount_exactness",
    "determination_consistency",
    "score_claims_extras",
]
