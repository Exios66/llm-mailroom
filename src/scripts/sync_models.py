#!/usr/bin/env python3
"""Sync the mailroom cost model prices into the Langfuse model registry.

Langfuse infers generation cost at ingestion time from its model registry.
Without an entry for an OpenRouter model (e.g. `qwen/qwen3.7-flash`), the UI
shows no cost for that model even though usage is recorded. This script makes
`config/taxonomy.yaml` -> `cost_models:` the single source of truth for prices
and pushes them into the registry as per-token USD prices (TOKENS unit).

- Idempotent: entries whose price already matches are left untouched.
- `--force` recreates every mailroom entry (delete + create) even when prices
  are unchanged (e.g. after a Langfuse platform change).
- Langfuse-managed entries are never touched.

Note: prices apply to *new* generations only — Langfuse computes cost at
ingestion time. Historical cost for a run is always available via the
`estimated_cost_usd` score attached to every trace.

Usage:
    python scripts/sync_models.py              # sync all cost models
    python scripts/sync_models.py --dry-run    # show what would change
    python scripts/sync_models.py --force      # recreate entries regardless
    python scripts/sync_models.py --model qwen/qwen3.7-flash
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

import structlog

logger = structlog.get_logger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from pipeline.env import load_env  # noqa: E402

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

_USD_PER_MILLION = 1_000_000


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing or unreachable) — nothing to sync.")
        return None
    return client


def _cost_models() -> dict[str, dict]:
    from pipeline.config import load_config

    try:
        return load_config().get("cost_models", {}) or {}
    except Exception:
        logger.exception("cost_models_load_failed")
        return {}


def _match_pattern(model_name: str) -> str:
    return rf"(?i)^({re.escape(model_name)})$"


def _existing_by_name(client) -> dict[str, object]:
    """Map model_name -> registry Model for every user-defined entry.

    Paginates: the registry holds 100+ Langfuse-managed defaults, so the
    user-defined entries can land on later pages.
    """
    existing = {}
    page_no = 1
    while True:
        page = client.api.models.list(page=page_no, limit=100)
        entries = page.data or []
        for m in entries:
            if getattr(m, "is_langfuse_managed", False):
                continue
            existing[m.model_name] = m
        if len(entries) < 100:
            break
        page_no += 1
    return existing


def _prices_match(model, *, input_per_million: float, output_per_million: float) -> bool:
    in_pt = input_per_million / _USD_PER_MILLION
    out_pt = output_per_million / _USD_PER_MILLION
    unit = getattr(model, "unit", None)
    if unit is not None:
        unit = getattr(unit, "value", unit)
    if unit != "TOKENS":
        return False
    return float(model.input_price or 0) == in_pt and float(model.output_price or 0) == out_pt


def sync_models(client, *, dry_run: bool = False, force: bool = False, only: str | None = None) -> int:
    from langfuse.api.commons.types.model_usage_unit import ModelUsageUnit

    cost_models = _cost_models()
    if not cost_models:
        print("No cost_models defined in taxonomy.yaml — nothing to sync.")
        return 0
    if only:
        if only not in cost_models:
            print(f"Unknown model '{only}' (not in taxonomy.yaml cost_models).")
            return 1
        cost_models = {only: cost_models[only]}

    existing = _existing_by_name(client)
    synced = 0
    for model_name, spec in sorted(cost_models.items()):
        in_m = float(spec.get("input_per_million", 0))
        out_m = float(spec.get("output_per_million", 0))
        current = existing.get(model_name)

        action = "unchanged"
        if force or current is None:
            action = "create"
        elif not _prices_match(current, input_per_million=in_m, output_per_million=out_m):
            action = "update"
        verb = "would " if dry_run else ""
        print(f"{verb}{action:<9} {model_name}  (${in_m:.4f}/{out_m:.4f} per 1M tokens)")
        if dry_run or action == "unchanged":
            if action == "unchanged":
                synced += 1
            continue

        if current is not None:
            client.api.models.delete(current.id)
        client.api.models.create(
            model_name=model_name,
            match_pattern=_match_pattern(model_name),
            unit=ModelUsageUnit.TOKENS,
            input_price=in_m / _USD_PER_MILLION,
            output_price=out_m / _USD_PER_MILLION,
        )
        synced += 1
    return synced


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync taxonomy.yaml cost_models into the Langfuse model registry.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing anything.")
    parser.add_argument("--force", action="store_true", help="Recreate entries even when prices are unchanged.")
    parser.add_argument("--model", help="Only sync this model (must be in taxonomy.yaml cost_models).")
    args = parser.parse_args()

    client = _client()
    if client is None:
        return 1

    synced = sync_models(client, dry_run=args.dry_run, force=args.force, only=args.model)

    if not args.dry_run and synced:
        from langfuse import get_client

        get_client().flush()
    print(f"\n{synced} model(s) synced to the Langfuse model registry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
