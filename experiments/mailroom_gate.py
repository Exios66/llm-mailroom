"""`langfuse/experiment-action` entry point: the mailroom regression gate.

Runs the hermetic ``mailroom-fixtures`` dataset through the pipeline with the
deterministic mock LLM and raises ``RegressionError`` when a gated metric falls
below the approved baseline (docs/superpowers/baselines/experiment-baseline.json)
or the run is empty/errored — the same fail-closed contract as
``src/scripts/run_experiment.py``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

BASELINE = ROOT / "docs" / "superpowers" / "baselines" / "experiment-baseline.json"


def experiment(context):
    from langfuse import RegressionError

    os.environ.setdefault("MAILROOM_RELATIONS_EMBEDDINGS", "0")
    from scripts import run_experiment as rx

    rows = rx.fixture_rows(None)
    rx.stage_fixture_samples(rows)
    items = rx.build_items(rows)
    run_id = datetime.now(timezone.utc).isoformat()
    result = context.run_experiment(
        name="mailroom PR gate",
        data=items,
        task=rx.make_task(True, run_id),
        evaluators=rx.ITEM_EVALUATORS,
        run_evaluators=[
            lambda *, item_results, **kw: rx.run_evaluators_from_rows(
                [rx._row_for(r.item, r.output) for r in item_results]
            )
        ],
    )
    summary = {
        "n_items": len(items),
        "n_errors": len(items) - len(result.item_results),
        "metrics": rx.compute_metrics([rx._row_for(r.item, r.output) for r in result.item_results]),
    }
    baseline = json.loads(BASELINE.read_text())
    problems = rx.check_regression(summary, baseline)
    if problems:
        metric = next((m for m in baseline["metrics"] if m not in summary["metrics"] or
                       summary["metrics"][m] < baseline["metrics"][m] - rx.DEFAULT_TOLERANCE), None)
        raise RegressionError(
            result=result,
            metric=metric,
            value=float(summary["metrics"].get(metric, 0.0)) if metric else 0.0,
            threshold=float(baseline["metrics"].get(metric, 1.0)) if metric else 1.0,
            message="; ".join(problems),
        )
    return result
