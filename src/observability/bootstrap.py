"""Bootstrap confidence intervals and small-sample delta testing.

Ported verbatim from ``llm-entity-extraction/src/bootstrap.py``. Small-sample
accuracy deltas are routinely over-read as real wins; this module formalizes
the variance: per-document scores are resampled (percentile bootstrap) into a
CI, and A/B deltas get a two-sample bootstrap difference CI with a
significance verdict — so a "win" needs a CI that excludes zero on the SAME
sample surface.

Everything is deterministic given a seed, offline, and dependency-free
(stdlib ``random`` only — no numpy/scipy requirement).
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
