"""Bootstrap confidence intervals and small-sample delta testing.

Ported from ``src/bootstrap.py`` (llm-entity-extraction). Deterministic given
a seed, offline, and dependency-free (stdlib ``random`` only — no numpy/scipy
requirement in the scoring path).
"""

from __future__ import annotations

import random
from typing import Any, Optional

DEFAULT_N_BOOT = 2000
DEFAULT_ALPHA = 0.05
DEFAULT_SEED = 42


def _clean(values: list[Any]) -> list[float]:
    """Coerce a per-document score list to floats, dropping None/non-numeric."""
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            out.append(1.0 if v else 0.0)
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _resample_means(values: list[float], n_boot: int, seed: int, rng: random.Random) -> list[float]:
    """Draw bootstrap sample means with replacement using the supplied RNG."""
    n = len(values)
    return [
        sum(values[i] for i in (rng.randrange(n) for _ in range(n))) / n
        for _ in range(n_boot)
    ]


def bootstrap_ci(
    values: list[Any],
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
) -> Optional[dict[str, Any]]:
    """Percentile-bootstrap 95% CI over per-document scores.

    Returns ``{"lo", "hi", "half", "n", "seed", "n_boot", "method"}`` (half =
    half-width in score points, 0-1 scale) or ``None`` when fewer than 2
    usable values (no CI on a single document).
    """
    values = _clean(values)
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = _resample_means(values, n_boot, seed, rng)
    means.sort()
    lo_idx = max(0, int(round((alpha / 2) * n_boot)) - 1)
    hi_idx = min(n_boot - 1, int(round((1 - alpha / 2) * n_boot)) - 1)
    lo = round(means[lo_idx], 4)
    hi = round(means[hi_idx], 4)
    return {
        "lo": lo,
        "hi": hi,
        "half": round((hi - lo) / 2, 4),
        "n": len(values),
        "seed": seed,
        "n_boot": n_boot,
        "method": "percentile-bootstrap",
    }


def delta_significance(
    values_a: list[Any],
    values_b: list[Any],
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
) -> Optional[dict[str, Any]]:
    """Two-sample bootstrap on the mean difference (B - A).

    Returns ``{"delta", "ci_lo", "ci_hi", "significant", "n_a", "n_b",
    "seed", "n_boot", "method"}`` — ``significant`` is True when the 95% CI
    on the difference excludes zero. ``None`` when either side has fewer than
    2 usable values (the delta is then unmeasurable, not "insignificant").
    """
    a = _clean(values_a)
    b = _clean(values_b)
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(seed)
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)

    def _sample_mean(values: list[float]) -> float:
        """Return the mean of one same-sized bootstrap resample."""
        n = len(values)
        return sum(values[rng.randrange(n)] for _ in range(n)) / n

    diffs = sorted(_sample_mean(b) - _sample_mean(a) for _ in range(n_boot))
    lo_idx = max(0, int(round((alpha / 2) * n_boot)) - 1)
    hi_idx = min(n_boot - 1, int(round((1 - alpha / 2) * n_boot)) - 1)
    lo = diffs[lo_idx]
    hi = diffs[hi_idx]
    return {
        "delta": round(mean_b - mean_a, 4),
        "ci_lo": round(lo, 4),
        "ci_hi": round(hi, 4),
        "significant": lo > 0 or hi < 0,
        "n_a": len(a),
        "n_b": len(b),
        "seed": seed,
        "n_boot": n_boot,
        "method": "two-sample-percentile-bootstrap",
    }


def wilson_bounds(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Exact (unrounded) Wilson score bounds ``(lo, hi)`` for rate ``p`` over ``n`` (``n >= 1``).

    ``z`` must be non-negative: a negative one would swap the bounds and
    understate the upper one a budget gate reads.
    """
    if not z >= 0:
        raise ValueError(f"z must be non-negative; got {z!r}")
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def round_up(value: float, places: int) -> float:
    """``value`` rounded to ``places`` decimals, never below ``value``.

    Float noise just above a decimal (``0.00010000000000000002``) rounds up a whole
    step: conservative for an upper bound compared with a budget.
    """
    rounded = round(value, places)
    if rounded < value:
        rounded = round(rounded + 10.0 ** -places, places)
    return rounded


def round_down(value: float, places: int) -> float:
    """``value`` rounded to ``places`` decimals, never above ``value``."""
    rounded = round(value, places)
    if rounded > value:
        rounded = round(rounded - 10.0 ** -places, places)
    return rounded


def wilson_ci(p: float, n: int, z: float = 1.96) -> Optional[dict[str, Any]]:
    """Wilson score interval for an aggregate proportion.

    Used when only the aggregate rate ``p`` and support ``n`` are available
    (e.g. rows of an Excel results workbook) rather than per-document scores.
    Returns ``{"lo", "hi", "half", "n", "p", "z"}`` or ``None`` when ``n < 1``.

    The bounds are rounded outward (``lo`` down, ``hi`` up) to 4 places, so the
    reported interval always contains the exact one and a bound compared with a
    budget never passes where the exact bound fails (#160). Gates that need the
    exact value use :func:`wilson_bounds`.
    """
    if n < 1 or p is None:
        return None
    p = float(p)
    exact_lo, exact_hi = wilson_bounds(p, n, z)
    lo, hi = round_down(exact_lo, 4), round_up(exact_hi, 4)
    return {
        "lo": lo,
        "hi": hi,
        "half": round((hi - lo) / 2, 4),
        "n": int(n),
        "p": round(p, 4),
        "z": z,
    }


__all__ = ["bootstrap_ci", "delta_significance", "wilson_ci",
           "DEFAULT_N_BOOT", "DEFAULT_ALPHA", "DEFAULT_SEED"]
