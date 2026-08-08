#!/usr/bin/env python3
"""Sync the mailroom health dashboards into Langfuse (idempotent).

Declares the quality widgets that surface quality declines automatically:
average score, p95 latency, and total cost per prompt over time (all
LINE_TIME_SERIES, bucketed by time so a regression shows up as a trend).

Also wires the LLM-as-a-Judge infrastructure widgets (throughput / P95 / P99
latency / errors) onto the existing "Production Health — Judges" dashboard.

- Idempotent: widgets/dashboards are matched by name; existing ones are
  updated in place when their config drifted, and placements are restored.
- Dashboard definitions live in version control per Langfuse best practice;
  re-running this script is always safe.

Usage:
    python scripts/sync_dashboards.py              # sync everything
    python scripts/sync_dashboards.py --dry-run    # show what would change
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from pipeline.env import load_env  # noqa: E402

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

# Our runs are tagged with environment `live` (watcher/API/ops) or `pilot`
# (scripts/run_pilot.py); `mock`/`misc` runs carry no real scores. The quality
# widgets scope to the two real environments.
REAL_ENVS_FILTER = {"column": "environment", "operator": "any of", "type": "stringOptions", "value": ["live", "pilot"]}

JUDGE_ENV_FILTER = {"column": "environment", "operator": "=", "type": "string", "value": "langfuse-llm-as-a-judge"}
NO_OPENAI_FILTER = {"column": "providedModelName", "operator": "does not contain", "type": "string", "value": "openai"}
ERROR_FILTER = {"column": "level", "operator": "=", "type": "string", "value": "ERROR"}


@dataclass
class WidgetSpec:
    name: str
    view: str
    dimensions: list[str]
    metrics: list[tuple[str, str]]
    chart_type: str
    filters: list[dict] = field(default_factory=list)
    description: str = ""


QUALITY_WIDGETS = [
    WidgetSpec(
        name="Mailroom Avg Score per Prompt over Time",
        view="scores-numeric",
        dimensions=["observationPromptName"],
        metrics=[("value", "avg")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER],
        description="Average quality score per prompt over time — a declining trend flags quality regressions early.",
    ),
    WidgetSpec(
        name="Mailroom p95 Latency per Prompt over Time",
        view="observations",
        dimensions=["promptName"],
        metrics=[("latency", "p95")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER],
        description="p95 latency per prompt over time.",
    ),
    WidgetSpec(
        name="Mailroom Total Cost per Prompt over Time",
        view="observations",
        dimensions=["promptName"],
        metrics=[("totalCost", "sum")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER],
        description="Total generation cost per prompt over time.",
    ),
]

JUDGE_WIDGETS = [
    WidgetSpec(
        name="Judge Throughput (Qwen & DeepSeek)",
        view="observations",
        dimensions=["providedModelName"],
        metrics=[("count", "count")],
        chart_type="BAR_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, NO_OPENAI_FILTER],
        description="LLM-as-a-judge evaluation volume per model.",
    ),
    WidgetSpec(
        name="Judge P95 Latency (Qwen & DeepSeek)",
        view="observations",
        dimensions=["providedModelName"],
        metrics=[("latency", "p95")],
        chart_type="LINE_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, NO_OPENAI_FILTER],
        description="p95 latency of LLM-as-a-judge evaluations per model.",
    ),
    WidgetSpec(
        name="Judge P99 Latency (Qwen & DeepSeek)",
        view="observations",
        dimensions=["providedModelName"],
        metrics=[("latency", "p99")],
        chart_type="LINE_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, NO_OPENAI_FILTER],
        description="p99 latency of LLM-as-a-judge evaluations per model.",
    ),
    WidgetSpec(
        name="Judge Errors (Qwen & DeepSeek)",
        view="observations",
        dimensions=["providedModelName"],
        metrics=[("count", "count")],
        chart_type="BAR_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, NO_OPENAI_FILTER, ERROR_FILTER],
        description="LLM-as-a-judge evaluation errors per model.",
    ),
]

QUALITY_DASHBOARD = {
    "name": "Mailroom Quality — per Prompt over Time",
    "description": "Quality health per prompt over time: average score, p95 latency, and total cost. A declining score trend shows up here automatically (run scripts/sync_dashboards.py to recreate).",
}

JUDGE_DASHBOARD = {
    "name": "Production Health — Judges (Qwen & DeepSeek)",
    "description": "Production health for LLM-as-a-judge evaluations using qwen and deepseek models: throughput, latency (P95/P99), and errors.",
}


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing or unreachable) — nothing to sync.")
        return None
    return client


def _spec_to_request(spec: WidgetSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "view": spec.view,
        "dimensions": [{"field": d} for d in spec.dimensions],
        "metrics": [{"measure": m, "agg": a} for m, a in spec.metrics],
        "filters": list(spec.filters),
        "chart_type": spec.chart_type,
    }


def _widget_signature(w) -> tuple:
    return (
        w.view,
        tuple(d.field for d in w.dimensions),
        tuple((m.measure, str(m.agg)) for m in w.metrics),
        str(w.chart_type),
        tuple(
            (f.column, f.operator, f.type, json_dumps(getattr(f, "value", None)), getattr(f, "key", None))
            for f in w.filters
        ),
    )


def json_dumps(v) -> str:
    import json

    return json.dumps(v, sort_keys=True, default=str)


def sync_widgets(client, specs: list[WidgetSpec], *, dry_run: bool) -> dict[str, str]:
    by_name = {w.name: w for w in client.api.unstable.dashboard_widgets.list().data}
    ids: dict[str, str] = {}
    for spec in specs:
        req = _spec_to_request(spec)
        existing = by_name.get(spec.name)
        if existing is None:
            if dry_run:
                print(f"create    {spec.name}")
            else:
                created = client.api.unstable.dashboard_widgets.create(**req)
                print(f"create    {spec.name} ({created.id})")
            ids[spec.name] = existing.id if existing else ""
            continue
        ids[spec.name] = existing.id
        if _widget_signature(existing) != _widget_signature_from_req(req):
            if dry_run:
                print(f"update    {spec.name}")
            else:
                client.api.unstable.dashboard_widgets.update(
                    existing.id,
                    name=req["name"],
                    description=req["description"],
                    view=req["view"],
                    dimensions=req["dimensions"],
                    metrics=req["metrics"],
                    filters=req["filters"],
                    chart_type=req["chart_type"],
                )
                print(f"update    {spec.name} ({existing.id})")
        else:
            print(f"unchanged {spec.name}")
    return ids


def _widget_signature_from_req(req: dict) -> tuple:
    return (
        req["view"],
        tuple(d["field"] for d in req["dimensions"]),
        tuple((m["measure"], m["agg"]) for m in req["metrics"]),
        req["chart_type"],
        tuple(
            (f["column"], f["operator"], f["type"], json_dumps(f.get("value")), f.get("key"))
            for f in req["filters"]
        ),
    )


def _existing_placements(client, dashboard_id: str) -> dict[str, tuple[str, int, int, int, int]]:
    d = client.api.unstable.dashboards.get(dashboard_id=dashboard_id)
    out: dict[str, tuple[str, int, int, int, int]] = {}
    for p in (d.definition.widgets if d.definition and d.definition.widgets else []):
        out[p.widget_id] = (p.id, p.x, p.y, p.width, p.height)
    return out


def _placement_kwargs(widget_id: str, x: int, y: int, width: int, height: int) -> dict:
    return {
        "type": "widget",
        "widget_id": widget_id,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


def sync_dashboard(client, spec: dict, widget_ids: dict[str, str], layout: list[tuple[str, int, int, int, int]], *, dry_run: bool) -> str:
    by_name = {d.name: d for d in client.api.unstable.dashboards.list().data}
    dashboard = by_name.get(spec["name"])
    if dashboard is None:
        if dry_run:
            print(f"create    dashboard {spec['name']}")
            return ""
        dashboard = client.api.unstable.dashboards.create(
            name=spec["name"], description=spec["description"]
        )
        print(f"create    dashboard {spec['name']} ({dashboard.id})")
    else:
        if dashboard.description != spec["description"]:
            if dry_run:
                print(f"update    dashboard {spec['name']}")
            else:
                client.api.unstable.dashboards.update(
                    dashboard.id,
                    name=spec["name"],
                    description=spec["description"],
                )
                print(f"update    dashboard {spec['name']} ({dashboard.id})")

    if dry_run:
        return dashboard.id or ""

    existing = _existing_placements(client, dashboard.id)
    for widget_name, x, y, width, height in layout:
        widget_id = widget_ids.get(widget_name)
        if not widget_id:
            print(f"skip      placement for missing widget {widget_name}")
            continue
        if widget_id in existing and existing[widget_id][1:] == (x, y, width, height):
            print(f"unchanged placement {widget_name}")
            continue
        if widget_id in existing:
            client.api.unstable.dashboards.delete_placement(
                dashboard_id=dashboard.id, placement_id=existing[widget_id][0]
            )
        kwargs = _placement_kwargs(widget_id, x, y, width, height)
        client.api.unstable.dashboards.add_placement(
            dashboard_id=dashboard.id, request=kwargs
        )
        print(f"place     {widget_name} ({widget_id})")
    return dashboard.id


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync mailroom dashboards to Langfuse.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without changing anything.")
    args = parser.parse_args()
    dry_run = args.dry_run

    client = _client()
    if client is None:
        return 1

    print(f"{'status':<10} resource")
    print("-" * 60)

    quality_ids = sync_widgets(client, QUALITY_WIDGETS, dry_run=dry_run)
    judge_ids = sync_widgets(client, JUDGE_WIDGETS, dry_run=dry_run)

    quality_layout = [
        (w.name, 0, y, 12, 6)
        for y, w in enumerate(
            [QUALITY_WIDGETS[0], QUALITY_WIDGETS[1], QUALITY_WIDGETS[2]]
        )
    ]
    judge_layout = [
        (JUDGE_WIDGETS[0].name, 0, 0, 6, 6),
        (JUDGE_WIDGETS[1].name, 6, 0, 6, 6),
        (JUDGE_WIDGETS[2].name, 0, 6, 6, 6),
        (JUDGE_WIDGETS[3].name, 6, 6, 6, 6),
    ]

    sync_dashboard(client, QUALITY_DASHBOARD, quality_ids, quality_layout, dry_run=dry_run)
    sync_dashboard(client, JUDGE_DASHBOARD, judge_ids, judge_layout, dry_run=dry_run)

    print("\nDone. Dashboards live in the Langfuse UI under Dashboards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
