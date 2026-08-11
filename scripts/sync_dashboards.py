#!/usr/bin/env python3
"""Sync the mailroom health dashboards into Langfuse (idempotent).

Four dashboards:

1. **Mailroom Quality — Scores over Time**: every numeric score type as its
   own series (trace-attached scores dimensioned by score name, NOT by
   prompt — the old prompt dimension produced one null bucket), plus p95
   latency and total cost per model.
2. **Production Health — Judges**: LLM-as-a-judge throughput / p95 / p99 /
   errors / tokens / cost per model (dimensioned by `model`, the requested
   qwen/deepseek string — the old providedModelName + negative-openai filter
   showed wrong models).
3. **Mailroom Quality — Completion / Correctness / Accuracy / Latency**
   (issue #2): completion (stage_completed, field presence, judge
   completeness), correctness (deterministic scores, judge correctness,
   classification rate, guardrails), accuracy, duration/latency, cost, LLM
   volume — each a single aggregate time series (all traces are named
   `document-pipeline`, so trace dimensions collapse).
4. **Mailroom Performance** (dashboard-correctness pass): throughput, errors,
   tokens per model, cost per model, p99 latency per model, run duration,
   cost, stage completion — the operational gauges for a true pipeline-status
   read.

Correctness fixes vs. the earlier definitions:
- score widgets dimension by score `name` (or none) — scores attach to the
  trace, never to observations/prompts
- model widgets use `model` (requested string) and a positive configured-models
  filter instead of providedModelName + "does not contain openai"
- removed invalid `docType` dimensions (not a widget dimension)
- model registry (`scripts/sync_models.py`) must be run once so cost widgets
  have prices (cost is computed at ingestion time)

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
# NOTE: dimension on `model` (the requested model string, e.g.
# qwen/qwen3.7-flash), NOT `providedModelName` — the OpenRouter adapter with
# with_default_models=true can report adapter-style names for non-custom
# models, and the old negative `does not contain openai` filter could hide the
# real models entirely (dashboard showed "incorrect models").
CONFIGURED_MODELS_FILTER = {
    "column": "model", "operator": "any of", "type": "stringOptions",
    "value": ["qwen/qwen3.7-flash", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"],
}
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


# Scores are attached to the TRACE (score_current_trace / create_score with
# trace_id) — never to an observation or prompt. Dimensioning score widgets by
# `observationPromptName` grouped everything into one null bucket (wrong
# numbers). `name` (the score name) is the correct dimension: each score type
# becomes its own series over time.
QUALITY_WIDGETS = [
    WidgetSpec(
        name="Mailroom Scores over Time (all score types)",
        view="scores-numeric",
        dimensions=["name"],
        metrics=[("value", "avg")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER],
        description="Average value of every numeric score type over time — parse_error, schema_valid, stage_completed, guardrail_triggered, confidences, ground-truth and judge scores. One series per score name.",
    ),
    WidgetSpec(
        name="Mailroom p95 Latency per Model over Time",
        view="observations",
        dimensions=["model"],
        metrics=[("latency", "p95")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER, CONFIGURED_MODELS_FILTER],
        description="p95 generation latency per model (qwen/deepseek) over time.",
    ),
    WidgetSpec(
        name="Mailroom Total Cost per Model over Time",
        view="observations",
        dimensions=["model"],
        metrics=[("totalCost", "sum")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER, CONFIGURED_MODELS_FILTER],
        description="Total generation cost per model over time (cost requires the model registry entry — see scripts/sync_models.py).",
    ),
]

JUDGE_WIDGETS = [
    WidgetSpec(
        name="Judge Throughput per Model",
        view="observations",
        dimensions=["model"],
        metrics=[("count", "count")],
        chart_type="BAR_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, CONFIGURED_MODELS_FILTER],
        description="LLM-as-a-judge evaluation volume per model (qwen/deepseek).",
    ),
    WidgetSpec(
        name="Judge P95 Latency per Model",
        view="observations",
        dimensions=["model"],
        metrics=[("latency", "p95")],
        chart_type="LINE_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, CONFIGURED_MODELS_FILTER],
        description="p95 latency of LLM-as-a-judge evaluations per model.",
    ),
    WidgetSpec(
        name="Judge P99 Latency per Model",
        view="observations",
        dimensions=["model"],
        metrics=[("latency", "p99")],
        chart_type="LINE_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, CONFIGURED_MODELS_FILTER],
        description="p99 latency of LLM-as-a-judge evaluations per model.",
    ),
    WidgetSpec(
        name="Judge Errors per Model",
        view="observations",
        dimensions=["model"],
        metrics=[("count", "count")],
        chart_type="BAR_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, CONFIGURED_MODELS_FILTER, ERROR_FILTER],
        description="LLM-as-a-judge evaluation errors per model.",
    ),
    WidgetSpec(
        name="Judge Total Tokens per Model",
        view="observations",
        dimensions=["model"],
        metrics=[("totalTokens", "sum")],
        chart_type="BAR_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, CONFIGURED_MODELS_FILTER],
        description="LLM-as-a-judge token spend per model.",
    ),
    WidgetSpec(
        name="Judge Cost per Model",
        view="observations",
        dimensions=["model"],
        metrics=[("totalCost", "sum")],
        chart_type="BAR_TIME_SERIES",
        filters=[JUDGE_ENV_FILTER, CONFIGURED_MODELS_FILTER],
        description="LLM-as-a-judge cost per model.",
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


# ---------------------------------------------------------------------------
# Tailored dimension views (issue #2 + dashboard-correctness pass): dedicated
# widgets for COMPLETION, CORRECTNESS, ACCURACY, LATENCY, and DURATION.
#
# Correctness notes (the earlier definitions showed wrong numbers):
#  - scores are attached to the TRACE, and every document trace is named
#    `document-pipeline`, so dimensioning by traceName/observationPromptName
#    collapses everything into one series. Time-series score widgets must use
#    NO dimension (single aggregate line over time).
#  - `docType` is not a valid Langfuse widget dimension — split-by-class views
#    are served by the per-score-name series in "Scores over Time" instead.
# ---------------------------------------------------------------------------

PILOT_ENV_FILTER = {"column": "environment", "operator": "any of", "type": "stringOptions", "value": ["pilot"]}


def _score_widget(name, score_name, agg="avg", dimensions=(), description=""):
    return WidgetSpec(
        name=name,
        view="scores-numeric",
        dimensions=list(dimensions),
        metrics=[("value", agg)],
        chart_type="LINE_TIME_SERIES",
        filters=[PILOT_ENV_FILTER, {"column": "name", "operator": "=", "type": "string", "value": score_name}],
        description=description,
    )


DIMENSION_WIDGETS = [
    # --- Completion (did the run reach a terminal stage? did all fields get extracted?)
    _score_widget(
        "Mailroom Completion — stage_completed rate over Time",
        "stage_completed",
        agg="avg",
        description="Fraction of runs reaching a terminal stage (archived/review/failed) over time.",
    ),
    _score_widget(
        "Mailroom Completion — expected_field_presence over Time",
        "expected_field_presence",
        agg="avg",
        description="Average fraction of required expected fields extracted non-empty (completeness of the extraction).",
    ),
    _score_widget(
        "Mailroom Completion — LLM-judge completeness over Time",
        "completeness",
        agg="avg",
        description="Offline LLM-as-a-judge completeness verdict (0-1) over time.",
    ),
    # --- Correctness (did we extract the right values / classify correctly?)
    _score_widget(
        "Mailroom Correctness — extraction_overall_score over Time",
        "extraction_overall_score",
        agg="avg",
        description="Deterministic field-scoring overall score (0-1) — extraction correctness vs ground truth.",
    ),
    _score_widget(
        "Mailroom Correctness — LLM-judge extraction_correctness over Time",
        "extraction_correctness",
        agg="avg",
        description="Offline LLM-as-a-judge factual-correctness verdict (0-1) over time.",
    ),
    _score_widget(
        "Mailroom Correctness — classification_correct rate over Time",
        "class_correct",
        agg="avg",
        description="Binary classification-correct rate against ground truth over time.",
    ),
    _score_widget(
        "Mailroom Correctness — guardrail_triggered rate over Time",
        "guardrail_triggered",
        agg="avg",
        description="Share of runs where a classification/extraction guardrail fired.",
    ),
    # --- Accuracy (aggregate accuracy over time)
    _score_widget(
        "Mailroom Accuracy — extraction_overall_score over Time",
        "extraction_overall_score",
        agg="avg",
        description="Deterministic extraction accuracy (0-1) over time.",
    ),
    # --- Latency / Duration (how long did runs take?)
    _score_widget(
        "Mailroom Duration — run_duration_seconds over Time",
        "run_duration_seconds",
        agg="avg",
        description="Average wall-clock pipeline duration (seconds) over time.",
    ),
    _score_widget(
        "Mailroom Latency — p95 run duration over Time",
        "run_duration_seconds",
        agg="p95",
        description="p95 pipeline duration over time.",
    ),
    _score_widget(
        "Mailroom Cost — estimated_cost_usd over Time",
        "estimated_cost_usd",
        agg="sum",
        description="Estimated LLM cost over time.",
    ),
    _score_widget(
        "Mailroom Volume — llm_call_count over Time",
        "llm_call_count",
        agg="avg",
        description="Average number of LLM calls per run over time (retry pressure).",
    ),
]


DIMENSION_DASHBOARD = {
    "name": "Mailroom Quality — Completion / Correctness / Accuracy / Latency",
    "description": "Tailored dimension views (issue #2 + dashboard-correctness pass): dedicated widgets for completion (stage_completed, field presence, judge completeness), correctness (deterministic field scores, judge correctness, classification rate, guardrails), accuracy, duration/latency, cost, and LLM-call volume. Scoped to pilot runs where ground truth exists. Scores are trace-attached, so each widget is a single aggregate series over time (trace names are all 'document-pipeline').",
}


# ---------------------------------------------------------------------------
# Pipeline Performance dashboard (dashboard-correctness pass): the operational
# gauges operators actually want — throughput, errors, tokens, cost, latency —
# over the real environments, dimensioned by model (the requested model
# string, not the adapter's providedModelName).
# ---------------------------------------------------------------------------

PERF_WIDGETS = [
    WidgetSpec(
        name="Throughput — documents per hour",
        view="traces",
        dimensions=[],
        metrics=[("count", "count")],
        chart_type="BAR_TIME_SERIES",
        filters=[REAL_ENVS_FILTER],
        description="Document pipeline runs per time bucket (live + pilot environments).",
    ),
    WidgetSpec(
        name="Error rate — ERROR observations over Time",
        view="observations",
        dimensions=[],
        metrics=[("count", "count")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER, ERROR_FILTER],
        description="ERROR-level observations over time — provider failures, guardrail triggers logged at error.",
    ),
    WidgetSpec(
        name="Total Tokens per Model over Time",
        view="observations",
        dimensions=["model"],
        metrics=[("totalTokens", "sum")],
        chart_type="BAR_TIME_SERIES",
        filters=[REAL_ENVS_FILTER, CONFIGURED_MODELS_FILTER],
        description="Token spend per model (qwen/deepseek) over time.",
    ),
    WidgetSpec(
        name="Cost per Model over Time",
        view="observations",
        dimensions=["model"],
        metrics=[("totalCost", "sum")],
        chart_type="BAR_TIME_SERIES",
        filters=[REAL_ENVS_FILTER, CONFIGURED_MODELS_FILTER],
        description="Generation cost per model over time (registry prices from scripts/sync_models.py).",
    ),
    WidgetSpec(
        name="p99 Latency per Model over Time",
        view="observations",
        dimensions=["model"],
        metrics=[("latency", "p99")],
        chart_type="LINE_TIME_SERIES",
        filters=[REAL_ENVS_FILTER, CONFIGURED_MODELS_FILTER],
        description="p99 generation latency per model over time.",
    ),
    WidgetSpec(
        name="Run Duration (score) over Time",
        view="scores-numeric",
        dimensions=[],
        metrics=[("value", "avg")],
        chart_type="LINE_TIME_SERIES",
        filters=[PILOT_ENV_FILTER, {"column": "name", "operator": "=", "type": "string", "value": "run_duration_seconds"}],
        description="Average wall-clock pipeline duration (seconds) over time.",
    ),
    WidgetSpec(
        name="Cost (score) over Time",
        view="scores-numeric",
        dimensions=[],
        metrics=[("value", "sum")],
        chart_type="LINE_TIME_SERIES",
        filters=[PILOT_ENV_FILTER, {"column": "name", "operator": "=", "type": "string", "value": "estimated_cost_usd"}],
        description="Estimated LLM cost per pilot run over time (backend-independent).",
    ),
    WidgetSpec(
        name="Stage completion rate over Time",
        view="scores-numeric",
        dimensions=[],
        metrics=[("value", "avg")],
        chart_type="LINE_TIME_SERIES",
        filters=[PILOT_ENV_FILTER, {"column": "name", "operator": "=", "type": "string", "value": "stage_completed"}],
        description="Fraction of runs reaching a terminal stage over time.",
    ),
]


PERF_DASHBOARD = {
    "name": "Mailroom Performance — Throughput / Errors / Tokens / Cost / Latency",
    "description": "Operational gauges over live + pilot runs: document throughput, error count, token spend per model, cost per model, p99 latency per model, run duration, cost, and stage completion. Model grouping uses the requested model string (qwen/deepseek), never the OpenRouter adapter's providedModelName.",
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
    dimension_ids = sync_widgets(client, DIMENSION_WIDGETS, dry_run=dry_run)
    perf_ids = sync_widgets(client, PERF_WIDGETS, dry_run=dry_run)

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
        (JUDGE_WIDGETS[4].name, 0, 12, 6, 6),
        (JUDGE_WIDGETS[5].name, 6, 12, 6, 6),
    ]
    dimension_layout = [
        (w.name, 0, y, 12, 5) for y, w in enumerate(DIMENSION_WIDGETS)
    ]
    perf_layout = [
        (w.name, 0, y, 12, 5) for y, w in enumerate(PERF_WIDGETS)
    ]

    sync_dashboard(client, QUALITY_DASHBOARD, quality_ids, quality_layout, dry_run=dry_run)
    sync_dashboard(client, JUDGE_DASHBOARD, judge_ids, judge_layout, dry_run=dry_run)
    sync_dashboard(client, DIMENSION_DASHBOARD, dimension_ids, dimension_layout, dry_run=dry_run)
    sync_dashboard(client, PERF_DASHBOARD, perf_ids, perf_layout, dry_run=dry_run)

    print("\nDone. Dashboards live in the Langfuse UI under Dashboards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
