"""Calibration, risk-bound, and discrimination math shared by scoring lanes.

These are pure functions over ``(score, correct)`` pairs. They own the
reviewer risk bar (#97 follow-on) and the extraction-gate ranking metrics:
an exact one-sided Clopper-Pearson upper bound for wrong auto-accepts, the
sample size that bound requires, ECE/Brier/reliability bins, AUROC, Spearman,
and a precision-constrained cutoff search.

No function here reads a run store, imports a consumer, or invents a missing
value. Empty or one-class populations return ``None``.
"""

from __future__ import annotations

import math
import numbers
import random
from statistics import NormalDist
from collections.abc import Iterable, Sequence
from typing import Any

from .bootstrap import wilson_ci

__all__ = [
    "clopper_pearson_upper",
    "required_samples",
    "cutoff_at_target",
    "cross_fit_cutoffs",
    "expected_calibration_error",
    "brier_score",
    "reliability_bins",
    "auroc",
    "spearman",
    "cutoff_for_precision",
]

Pair = tuple[float, bool]

#: Largest accept count :func:`required_samples` searches before giving up.
_MAX_REQUIRED_SAMPLES = 1_000_000


def _validate_count(value: int, name: str, *, upper: int | None = None) -> int:
    """Require a non-negative integer, optionally bounded by *upper*."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"{name} must be an integer")
    value = int(value)  # NumPy / pandas integral counts
    if value < 0 or (upper is not None and value > upper):
        raise ValueError(f"{name} must be between 0 and {upper if upper is not None else 'infinity'}")
    return value


def _validate_confidence(confidence: float) -> float:
    """Require a probability strictly inside ``(0, 1)``."""
    value = float(confidence)
    if not 0.0 < value < 1.0:
        raise ValueError(f"confidence must be in (0, 1); got {confidence!r}")
    return value


def clopper_pearson_upper(
    errors: int, n: int, *, confidence: float = 0.95
) -> float | None:
    """Exact one-sided upper bound on an error rate.

    Solves ``P(X <= errors; n, p) = 1 - confidence`` for ``p``. This is the
    bound the reviewer qualification bar uses: zero errors in 299 accepts
    gives 0.997%, while zero in 149 gives 1.99%. Returns ``None`` for ``n=0``.
    """
    errors = _validate_count(errors, "errors")
    n = _validate_count(n, "n")
    if n == 0:
        return None
    if errors > n:
        raise ValueError(f"errors ({errors}) cannot exceed n ({n})")
    level = _validate_confidence(confidence)
    if errors == n:
        return 1.0
    # The exact bound is the ``confidence`` quantile of Beta(errors + 1, n - errors).
    # The regularized incomplete-beta inverse needs no binomial coefficients,
    # so large counts cannot overflow a float (``math.comb(10**6, 67)`` does).
    from scipy.special import betaincinv

    return float(betaincinv(errors + 1, n - errors, level))


def required_samples(
    errors: int, *, risk: float = 0.01, confidence: float = 0.95
) -> int | None:
    """Smallest number of accepts whose exact upper bound clears *risk*.

    Zero errors need 299 accepts for a 1% bar at 95% confidence; one error
    needs 473. ``None`` means no sample up to one million accepts clears it.
    """
    errors = _validate_count(errors, "errors")
    risk = _validate_confidence(risk)
    confidence = _validate_confidence(confidence)
    if errors == 0:
        # 1 - (1 - risk) ** n <= 1 - confidence
        # log1p keeps a tiny risk from rounding ``1 - risk`` to 1.0 (a zero
        # denominator); the cap applies here as it does to the search below.
        exact = math.log(1.0 - confidence) / math.log1p(-risk)
        # Compare before ``ceil``: a risk near the float minimum overflows to inf.
        if not math.isfinite(exact) or exact > _MAX_REQUIRED_SAMPLES:
            return None
        return max(1, math.ceil(exact))
    low, high = max(errors, 1), _MAX_REQUIRED_SAMPLES
    if errors > high:
        # More errors than the search cap allows trials: no sample clears it.
        return None
    if (clopper_pearson_upper(errors, high, confidence=confidence) or 1.0) > risk:
        return None
    while low < high:
        mid = (low + high) // 2
        bound = clopper_pearson_upper(errors, mid, confidence=confidence)
        if bound is not None and bound <= risk:
            high = mid
        else:
            low = mid + 1
    return low


def _accepted(rows: Sequence[Pair], cutoff: float) -> tuple[int, int]:
    """(correct, accepted) at one inclusive confidence cutoff."""
    kept = [correct for confidence, correct in rows if confidence >= cutoff]
    return sum(kept), len(kept)


def cutoff_at_target(rows: Sequence[Pair], target: float) -> float | None:
    """Lowest confidence whose accepted set reaches *target* accuracy.

    Walks the rows from highest to lowest confidence once, so a 3,000-document
    reviewer class stays cheap. Returns ``None`` when no positive-confidence
    cutoff reaches the target.
    """
    if not 0.0 < target <= 1.0:
        raise ValueError(f"target must be in (0, 1]; got {target!r}")
    ordered = sorted(rows, key=lambda pair: -pair[0])
    best: float | None = None
    correct = 0
    for index, (confidence, hit) in enumerate(ordered, 1):
        correct += int(bool(hit))
        if index < len(ordered) and ordered[index][0] == confidence:
            continue
        if confidence > 0 and correct / index >= target:
            best = confidence
    return best


def cross_fit_cutoffs(
    rows: Sequence[Pair],
    *,
    target: float = 0.95,
    folds: int = 5,
    seed: int = 42,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Tune a cutoff on four folds and score the held-out fifth, every row once.

    This is the cross-fit the sandbox used for reviewer cutoffs. It reports the
    pooled held-out accepted count/accuracy and a Wilson interval; a class that
    never reaches the target gets ``cutoff=None`` and no held-out metrics.
    """
    if folds < 2:
        raise ValueError("folds must be at least 2")
    _validate_confidence(confidence)
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    cutoff = cutoff_at_target(shuffled, target)
    if cutoff is None:
        return {
            "cutoff": None,
            "holdout_documents": len(shuffled),
            "holdout_accepted": 0,
            "holdout_accuracy": None,
            "holdout_accuracy_ci": None,
            "holdout_share_accepted": None,
            "meets_target": False,
        }
    right = kept = 0
    for fold in range(folds):
        held = shuffled[fold::folds]
        training = [row for index, row in enumerate(shuffled) if index % folds != fold]
        fold_cutoff = cutoff_at_target(training, target)
        if fold_cutoff is None:
            continue
        fold_right, fold_kept = _accepted(held, fold_cutoff)
        right += fold_right
        kept += fold_kept
    z = NormalDist().inv_cdf(0.5 + _validate_confidence(confidence) / 2.0)
    interval = wilson_ci(right / kept, kept, z=z) if kept else None
    return {
        "cutoff": cutoff,
        "holdout_documents": len(shuffled),
        "holdout_accepted": kept,
        "holdout_accuracy": right / kept if kept else None,
        "holdout_accuracy_ci": interval,
        "holdout_share_accepted": kept / len(shuffled) if shuffled else None,
        # Decide on the exact bound: ``wilson_ci`` rounds ``lo`` to 4 places,
        # which can lift a just-short bound over the target.
        "meets_target": bool(kept and _wilson_lower(right / kept, kept, z) >= target),
    }


def _wilson_lower(p: float, n: int, z: float) -> float:
    """Unrounded Wilson lower bound (the same formula as :func:`wilson_ci`)."""
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - margin)


def _clean_pairs(pairs: Iterable[Pair]) -> list[Pair]:
    """Keep finite confidences in [0, 1] and boolean correctness only."""
    clean: list[Pair] = []
    for confidence, correct in pairs:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        value = float(confidence)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            continue
        clean.append((value, bool(correct)))
    return clean


def expected_calibration_error(pairs: Iterable[Pair], *, bins: int = 10) -> float | None:
    """Equal-width ECE: weighted mean |mean confidence - accuracy| per bin."""
    if bins < 1:
        raise ValueError("bins must be positive")
    clean = _clean_pairs(pairs)
    if not clean:
        return None
    buckets: list[list[Pair]] = [[] for _ in range(bins)]
    for confidence, correct in clean:
        index = min(int(confidence * bins), bins - 1)
        buckets[index].append((confidence, correct))
    total = len(clean)
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        mean_confidence = sum(value for value, _ in bucket) / len(bucket)
        accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
        error += (len(bucket) / total) * abs(mean_confidence - accuracy)
    return round(error, 6)


def brier_score(pairs: Iterable[Pair]) -> float | None:
    """Mean squared difference between confidence and correctness."""
    clean = _clean_pairs(pairs)
    if not clean:
        return None
    return round(
        sum((confidence - float(correct)) ** 2 for confidence, correct in clean) / len(clean),
        6,
    )


def reliability_bins(pairs: Iterable[Pair], *, bins: int = 10) -> list[dict[str, Any]]:
    """Per-bin count, mean confidence, accuracy, and calibration gap."""
    if bins < 1:
        raise ValueError("bins must be positive")
    clean = _clean_pairs(pairs)
    out: list[dict[str, Any]] = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [
            (confidence, correct)
            for confidence, correct in clean
            if low <= confidence < high or (index == bins - 1 and confidence == 1.0)
        ]
        if not bucket:
            out.append(
                {
                    "bin": index,
                    "low": round(low, 6),
                    "high": round(high, 6),
                    "n": 0,
                    "mean_confidence": None,
                    "accuracy": None,
                    "gap": None,
                }
            )
            continue
        mean_confidence = sum(value for value, _ in bucket) / len(bucket)
        accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
        out.append(
            {
                "bin": index,
                "low": round(low, 6),
                "high": round(high, 6),
                "n": len(bucket),
                "mean_confidence": round(mean_confidence, 6),
                "accuracy": round(accuracy, 6),
                "gap": round(abs(mean_confidence - accuracy), 6),
            }
        )
    return out


def _is_nan(value: Any) -> bool:
    """NaN test that never converts: ``math.isnan(10**400)`` overflows."""
    return value != value


def _rank(values: Sequence[float]) -> list[float]:
    """Average ranks with ties sharing the midpoint."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        rank = (index + end) / 2.0 + 1.0
        for cursor in range(index, end + 1):
            ranks[order[cursor]] = rank
        index = end + 1
    return ranks


def auroc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    """Probability a positive outranks a negative, with ties counting half.

    Returns ``None`` when either class is absent (no discrimination to measure).
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    pairs = [
        (score, bool(label))
        for score, label in zip(scores, labels)
        # numbers.Real admits numpy scalars (float32, int64); bools are labels, not scores.
        if isinstance(score, numbers.Real)
        and not isinstance(score, bool)
        and type(score).__name__ != "bool_"
        and not _is_nan(score)  # NaN has no order, so it cannot be ranked
    ]
    positives = [score for score, label in pairs if label]
    negatives = [score for score, label in pairs if not label]
    if not positives or not negatives:
        return None
    ranks = _rank([score for score, _ in pairs])
    positive_rank_sum = sum(rank for rank, (_, label) in zip(ranks, pairs) if label)
    count = len(positives) * len(negatives)
    return round((positive_rank_sum - len(positives) * (len(positives) + 1) / 2.0) / count, 6)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation; ``None`` under three rows or with a constant axis."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have the same length")
    # NaN has no order: drop any pair carrying one before ranking.
    # Rank the values as given: float() would merge distinct integers above 2**53.
    kept = [
        (x, y)
        for x, y in zip(xs, ys)
        if not (_is_nan(x) or _is_nan(y))
    ]
    if len(kept) < 3:
        return None
    rx, ry = _rank([x for x, _ in kept]), _rank([y for _, y in kept])
    mean_x, mean_y = sum(rx) / len(rx), sum(ry) / len(ry)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    variance = math.sqrt(
        sum((a - mean_x) ** 2 for a in rx) * sum((b - mean_y) ** 2 for b in ry)
    )
    return round(covariance / variance, 6) if variance else None


def cutoff_for_precision(
    scores: Sequence[float], labels: Sequence[bool], precision: float
) -> dict[str, Any]:
    """Lowest inclusive cutoff whose accepted set keeps *precision*.

    Returns the cutoff, observed precision, accept rate, and recall. When no
    cutoff reaches the target, ``cutoff`` and ``precision`` are ``None``.
    """
    if not 0.0 < precision <= 1.0:
        raise ValueError(f"precision must be in (0, 1]; got {precision!r}")
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    pairs = list(zip((float(score) for score in scores), (bool(label) for label in labels)))
    total_positive = sum(1 for _, label in pairs if label)
    for cutoff in sorted({score for score, _ in pairs}):
        accepted = [label for score, label in pairs if score >= cutoff]
        if accepted and sum(accepted) / len(accepted) >= precision:
            return {
                # Unrounded: rounding could admit a lower-scoring row that
                # the reported precision excluded.
                "cutoff": cutoff,
                "precision": round(sum(accepted) / len(accepted), 6),
                "accept_rate": round(len(accepted) / len(pairs), 6) if pairs else None,
                "recall": round(sum(accepted) / total_positive, 6) if total_positive else None,
            }
    return {"cutoff": None, "precision": None, "accept_rate": 0.0, "recall": 0.0}
