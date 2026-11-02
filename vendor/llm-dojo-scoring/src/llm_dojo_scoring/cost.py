"""Per-model token pricing, cost estimation, and usage aggregation.

Ported from ``src/cost_models.py`` + ``src/experiment_log.py::tokens_summary``
(llm-entity-extraction) so any project can compute cost deterministically from
token counts x verified per-model prices. Prices default to the OpenRouter
list prices; override via settings (``cost_models``) or YAML.
"""

from __future__ import annotations

from typing import Any, Optional

from .config import get_settings
from .scorecard_honesty import COST_BASIS_VALUES


def price_for(model: Optional[str]) -> Optional[tuple[float, float]]:
    """Resolve a model string to its per-1M-token prices (exact, then
    prefix-matched so a dated ``qwen/qwen3.7-flash-20260727`` rolls to the
    base price). Returns None for unknown models — an honest "unknown price",
    never a fabricated number."""
    if not model:
        return None
    model = str(model).strip()
    prices = get_settings().cost_models
    if model in prices:
        return prices[model]
    for known, price in prices.items():
        if model.startswith(known):
            return price
    return None


def resolve_cost_basis(
    records: list[dict] | dict | None,
    *,
    default: str = "busy_window",
) -> str:
    """Single ``COST_BASIS_VALUES`` label for a table; never mix silently.

    Wholly unlabeled inputs keep ``default``. A table that mixes labeled
    rows with unlabeled rows, or two different labels, raises.
    """
    rows: list[dict]
    if records is None:
        rows = []
    elif isinstance(records, dict):
        rows = [records]
    else:
        rows = list(records)
    labeled: list[str] = []
    n_unlabeled = 0
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        raw = rec.get("cost_basis") or rec.get("usd_basis")
        if not raw:
            n_unlabeled += 1
            continue
        basis = str(raw)
        if basis not in COST_BASIS_VALUES:
            raise ValueError(
                f"cost_basis must be one of {sorted(COST_BASIS_VALUES)}, got {basis!r}"
            )
        labeled.append(basis)
    unique = set(labeled)
    if not unique:
        return default
    if n_unlabeled:
        raise ValueError(
            "cost_basis mixed: labeled and unlabeled records; "
            "never mix a labeled basis with unlabeled rows in one table"
        )
    if len(unique) > 1:
        raise ValueError(
            "cost_basis mixed "
            f"{sorted(unique)}; never mix busy_window with billed_incl_cold in one table"
        )
    return unique.pop()


def estimate_cost(
    prompt_tokens: Optional[Any],
    completion_tokens: Optional[Any],
    model: Optional[str],
) -> Optional[float]:
    """USD cost for one run's token counts, or None when the model's price
    is unknown or no tokens were recorded."""
    prices = price_for(model)
    if prices is None:
        return None
    try:
        prompt = int(prompt_tokens or 0)
        completion = int(completion_tokens or 0)
    except (TypeError, ValueError):
        return None
    if prompt + completion <= 0:
        return None
    per_million_in, per_million_out = prices
    return round(prompt * per_million_in / 1_000_000
                 + completion * per_million_out / 1_000_000, 6)


def estimate_for_record(record: dict) -> dict[str, Any]:
    """Cost estimate for an experiment-log record (stage-aware).

    Returns ``{"cost_estimated_usd", "per_doc_usd", "prompt_tokens",
    "completion_tokens", "model", "price_source"}``.
    """
    tokens = record.get("tokens") or {}
    if "total" in tokens:
        bucket = tokens["total"] or {}
    else:
        bucket = tokens
    prompt = bucket.get("prompt_tokens")
    completion = bucket.get("completion_tokens")
    model = record.get("model")
    cost = estimate_cost(prompt, completion, model)
    per_doc = None
    if cost is not None:
        n_rows = record.get("n_rows") or 0
        per_doc = round(cost / n_rows, 6) if n_rows else None
    basis = resolve_cost_basis(record)
    return {
        "cost_estimated_usd": cost,
        "per_doc_usd": per_doc,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "model": model,
        "price_source": price_for(model),
        "cost_basis": basis,
    }


def tokens_summary(usage_records: list[dict], model: str | None = None) -> dict:
    """Aggregate per-row usage dicts into one tokens/cost summary.

    Each usage record comes from an agent's ``_last_usage``:
    ``{prompt_tokens, completion_tokens, total_tokens, cost}``. Rows replayed
    from a manifest carry no usage (they were paid for in the original run).
    ``model`` enables deterministic cost scoring.
    """
    prompt = completion = total = 0
    cost_values: list[float] = []
    rows = 0
    for usage in usage_records or []:
        if not isinstance(usage, dict) or not usage:
            continue
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        total += int(usage.get("total_tokens") or 0)
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            cost_values.append(float(cost))
        rows += 1
    cost_estimated = None
    if model:
        cost_estimated = estimate_cost(prompt, completion, model)
    cost_basis = resolve_cost_basis(usage_records)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost_usd": round(sum(cost_values) / len(cost_values), 6) if cost_values else 0.0,
        "cost_total_usd": round(sum(cost_values), 6),
        "cost_estimated_usd": cost_estimated,
        "cost_basis": cost_basis,
        "rows_with_usage": rows,
    }


__all__ = [
    "price_for",
    "estimate_cost",
    "estimate_for_record",
    "tokens_summary",
    "resolve_cost_basis",
]
