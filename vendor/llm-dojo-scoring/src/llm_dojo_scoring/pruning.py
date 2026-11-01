"""Tier-based pruning — the dashboard-facing filter layer (KANBAN-061).

One import for every "show me what matters" query:

- :func:`prune_metrics` — registry metrics under a tier cap (T0+T1 default).
- :func:`dashboard_metrics` / :func:`headline_metrics` — profile-aware:
  resolve the agent's bundle, intersect with the tier cap.
- :func:`prune_records` — filter already-emitted score records by tier.

The 2am discipline: dashboards default to ``max_tier=1``; everything deeper
is opt-in exploration, never default noise.
"""

from __future__ import annotations

from typing import Iterable

from .bundles import bundle_metric_names
from .profiles import load_profiles
from .registry import MetricDef, MetricTier, Registry, load_registry

__all__ = [
    "DEFAULT_DASHBOARD_TIER",
    "prune_metrics",
    "dashboard_metrics",
    "headline_metrics",
    "prune_records",
]

DEFAULT_DASHBOARD_TIER = 1


def prune_metrics(
    *,
    max_tier: int | MetricTier = DEFAULT_DASHBOARD_TIER,
    agent: str | None = None,
    registry: Registry | None = None,
) -> list[MetricDef]:
    """Registry metrics at or above the tier cap, ordered tier then name."""
    return (registry or load_registry()).filter(max_tier=max_tier, agent=agent)


def dashboard_metrics(
    agent: str,
    *,
    max_tier: int | MetricTier = DEFAULT_DASHBOARD_TIER,
    registry: Registry | None = None,
) -> list[str]:
    """Metric names an agent's dashboard panel shows by default.

    Intersects the agent's profile bundle with the tier cap — a bundle
    metric deeper than the cap is pruned; a registry metric outside the
    bundle never appears. Agents without a usable profile fall back to
    every registry metric applicable to them.
    """
    reg = registry or load_registry()
    try:
        profile = load_profiles()[agent]
        names = bundle_metric_names(
            profile.resolve_bundle(registry=reg), agent=agent, registry=reg
        )
    except (KeyError, ValueError):
        return [m.name for m in reg.filter(max_tier=max_tier, agent=agent)]
    cap = MetricTier(max_tier)
    return [n for n in names if reg.get(n).tier <= cap]


def headline_metrics(agent: str, *, registry: Registry | None = None) -> list[str]:
    """Strictly T0 — the one-number-per-agent view."""
    return dashboard_metrics(agent, max_tier=MetricTier.HEADLINE, registry=registry)


def prune_records(
    records: Iterable[object],
    *,
    max_tier: int | MetricTier = DEFAULT_DASHBOARD_TIER,
    registry: Registry | None = None,
) -> list[object]:
    """Filter emitted score records down to the tier cap.

    Accepts any records exposing a ``metric`` attribute (e.g.
    :class:`llm_dojo_scoring.emitter.ScoreRecord`). Unknown metric names are
    dropped — pruning never guesses.
    """
    reg = registry or load_registry()
    cap = MetricTier(max_tier)
    out: list[object] = []
    for r in records:
        name = getattr(r, "metric", None)
        if name is None:
            continue
        try:
            if reg.get(name).tier <= cap:
                out.append(r)
        except KeyError:
            continue
    return out
