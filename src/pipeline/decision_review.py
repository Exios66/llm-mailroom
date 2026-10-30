"""Pure routing for a decision model acting as the Lane A reviewer.

A ``decision_only`` document reaches ``extract`` only when every condition
holds; each failed condition is recorded (not just the first) and any
failure routes to ``human_review``. Overrides never auto-accept. The
function does no I/O and reads no config: eligible classes and cutoffs are
passed in, so the graph and the evals call the same code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Collection, Literal, Mapping, Optional

from pipeline.reconsideration import (
    REVIEWER_DISAGREEMENT,
    REVIEWER_INELIGIBLE_LABEL,
    REVIEWER_LOW_CONFIDENCE,
    REVIEWER_NO_TEXT,
    REVIEWER_UNAVAILABLE,
)


@dataclass(frozen=True)
class DecisionAnswer:
    """A valid answer from the decision model."""

    doc_type: Optional[str]
    doc_subclass: Optional[str]
    confidence: Optional[float]
    model: str


@dataclass(frozen=True)
class ReviewRoute:
    destination: Literal["extract", "human_review"]
    causes: tuple[str, ...]
    verdict: Literal["decision_accepts", "decision_escalates"]


def _valid_confidence(value: object) -> bool:
    """Accept finite numeric values in [0, 1], excluding booleans.

    An integer too large to convert to a float raises OverflowError.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _meets_cutoff(confidence: object, cutoff: object) -> bool:
    """Return whether valid confidence meets or equals a valid cutoff.

    OverflowError from converting an oversized integer propagates.
    """
    if not _valid_confidence(confidence) or not _valid_confidence(cutoff):
        return False
    return float(confidence) >= float(cutoff)  # type: ignore[arg-type]


def _route(causes: list[str]) -> ReviewRoute:
    """Escalate when causes are present; otherwise accept for extraction."""
    if causes:
        return ReviewRoute("human_review", tuple(causes), "decision_escalates")
    return ReviewRoute("extract", (), "decision_accepts")


def decide_review(
    sorter_type: Optional[str],
    answer: Optional[DecisionAnswer],
    *,
    eligible: Collection[str],
    cutoffs: Mapping[str, float],
    has_text: bool,
) -> ReviewRoute:
    """Route a decision review and return its destination, causes, and verdict.

    Extraction requires text, matching sorter and reviewer labels in
    ``eligible``, and confidence at or above the reviewer's class cutoff.
    Confidence and cutoff must be finite numbers in [0, 1], excluding
    booleans. Missing or invalid cutoffs and confidence cause escalation.
    An absent answer records unavailability and, if applicable, missing text;
    otherwise all failed conditions are recorded in the spec's table order.
    Oversized integers that cannot be converted to float raise OverflowError.
    """
    causes: list[str] = []
    if answer is None:
        causes.append(REVIEWER_UNAVAILABLE)
        if not has_text:
            causes.append(REVIEWER_NO_TEXT)
        return _route(causes)

    if not has_text:
        causes.append(REVIEWER_NO_TEXT)
    if sorter_type not in eligible or answer.doc_type not in eligible:
        causes.append(REVIEWER_INELIGIBLE_LABEL)
    if sorter_type is None or sorter_type != answer.doc_type:
        causes.append(REVIEWER_DISAGREEMENT)
    cutoff = cutoffs.get(answer.doc_type) if answer.doc_type is not None else None
    if not _meets_cutoff(answer.confidence, cutoff):
        causes.append(REVIEWER_LOW_CONFIDENCE)
    return _route(causes)
