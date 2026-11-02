"""Exact deterministic pipeline verdict thresholds, independent of config I/O."""
from __future__ import annotations

from typing import Any


def deterministic_verdict_label(
    overall_score: float | None,
    *,
    needs_judge_review: bool = False,
    class_mismatch: bool = False,
    ambiguous_band: Any = None,
) -> str:
    """Preserve mismatch/review/None priority and inclusive pipeline thresholds.

    Malformed bands fall back to [0.5, 0.85]; reversed bands, score coercion,
    nonfinite values and booleans retain the original pipeline behavior.
    """
    if class_mismatch:
        return "MISS"
    if needs_judge_review:
        return "PARTIAL"
    if overall_score is None:
        return "PARTIAL"
    try:
        band = ambiguous_band or [0.5, 0.85]
        low, high = float(band[0]), float(band[1])
    except Exception:
        low, high = 0.5, 0.85
    score = float(overall_score)
    if score >= high:
        return "CORRECT"
    if score <= low:
        return "MISS"
    return "PARTIAL"
