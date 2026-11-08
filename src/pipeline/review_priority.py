"""Priority of a parked document in the human review tray.

Pure: no I/O, no clock reads. The weights live in ``taxonomy.yaml`` under
``review_queue`` so tuning the tray is a config change.

    severity = heaviest cause weight + 0.1 * (sum of the other cause weights)
    base     = severity * class_tier_weight[doc_type]
    score    = base * (1 + age_term),  age_term = 1 - 0.5 ** (age_hours / half_life)

``tier`` comes from ``base`` (age never promotes a tier, it only reorders within
one), and ``sla_breached`` compares age with that tier's ``sla_hours``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

DEFAULT_WEIGHT = 1.0
NO_CAUSE = "no_cause_recorded"


def _config(cfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if cfg is not None:
        return cfg
    from pipeline.config import load_config

    block = load_config().get("review_queue")
    return block if isinstance(block, dict) else {}


def _ts(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif value:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _causes(item: Mapping[str, Any]) -> list[str]:
    causes = item.get("review_causes")
    if not causes:
        review = (item.get("extracted_data") or {}).get("_review")
        causes = review.get("review_causes") if isinstance(review, dict) else None
    return [c for c in (causes or []) if isinstance(c, str) and c]


def age_hours(item: Mapping[str, Any], now: dt.datetime) -> float | None:
    """Hours since the document was parked (``updated_at``, else ``created_at``)."""
    since = _ts(item.get("updated_at")) or _ts(item.get("created_at"))
    if since is None:
        return None
    return max(0.0, (now - since).total_seconds() / 3600.0)


def priority(
    item: Mapping[str, Any], now: dt.datetime, *, cfg: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return ``{score, tier, reasons, age_hours, sla_hours, sla_breached}``."""
    conf = _config(cfg)
    weights = conf.get("cause_weights") or {}
    class_weights = conf.get("class_tier_weight") or {}
    cutoffs = conf.get("tier_cutoffs") or {}
    sla = conf.get("sla_hours") or {}
    half_life = float(conf.get("age_half_life_hours") or 24)

    causes = _causes(item)
    weighted = sorted(
        ((float(weights.get(c, DEFAULT_WEIGHT)), c) for c in causes), reverse=True
    )
    if weighted:
        severity = weighted[0][0] + 0.1 * sum(w for w, _ in weighted[1:])
        reasons = [c for _, c in weighted]
    else:
        severity, reasons = DEFAULT_WEIGHT, [NO_CAUSE]

    doc_type = item.get("doc_type")
    class_weight = float(class_weights.get(doc_type, 1.0)) if doc_type else 1.0
    base = severity * class_weight

    age = age_hours(item, now)
    age_term = 0.0 if age is None else 1.0 - 0.5 ** (age / half_life)
    score = base * (1.0 + age_term)

    if base >= float(cutoffs.get("high", 3.0)):
        tier = "high"
    elif base >= float(cutoffs.get("normal", 1.5)):
        tier = "normal"
    else:
        tier = "low"
    limit = sla.get(tier)
    return {
        "score": round(score, 4),
        "tier": tier,
        "reasons": reasons,
        "age_hours": None if age is None else round(age, 2),
        "sla_hours": limit,
        "sla_breached": bool(limit is not None and age is not None and age > float(limit)),
    }


def sort_queue(
    items: list[dict[str, Any]], now: dt.datetime, *, cfg: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Attach ``priority`` to each item and sort: highest score, then oldest first."""
    for item in items:
        item["priority"] = priority(item, now, cfg=cfg)
        item["sla_breached"] = item["priority"]["sla_breached"]
    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    return sorted(
        items,
        key=lambda d: (
            -d["priority"]["score"],
            _ts(d.get("updated_at")) or _ts(d.get("created_at")) or far,
        ),
    )


def oldest_age_hours(items: list[dict[str, Any]]) -> float | None:
    ages = [i["priority"]["age_hours"] for i in items if i.get("priority", {}).get("age_hours") is not None]
    return max(ages) if ages else None
