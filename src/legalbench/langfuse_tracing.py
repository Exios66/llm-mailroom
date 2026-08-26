"""Langfuse tracing for LegalBench runs.

One trace per run (deterministic seed = run id, so re-runs update the same
trace), one child observation per question/document, and run-level scores
attached through the pipeline's own score machinery
(``observability.scores``) so the scores are config-registered and visible
in Langfuse like every other production score. Everything no-ops when
observability is disabled (``OBSERVABILITY_PROVIDER=none``, tests, --mock).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from observability import tracing
from observability.scores import ensure_score_configs, score_trace

SCORE_NAMES = (
    "legalbench_accuracy",
    "legalbench_macro_f1",
    "legalbench_calibration_error",
    "legalbench_n_questions",
    "legalbench_task",
)


def _environment() -> str:
    return os.environ.get("OBSERVABILITY_ENVIRONMENT", "misc")


@contextmanager
def legalbench_trace(
    run_id: str,
    task_id: str,
    model: Optional[str] = None,
    n: int = 0,
    metadata: Optional[dict[str, Any]] = None,
) -> Iterator[Any]:
    """Open the per-run Langfuse trace (no-op when tracing is disabled)."""
    meta = {
        "suite": "legalbench",
        "task": task_id,
        "run_id": run_id,
        "model": model,
        "n": n,
        **(metadata or {}),
    }
    with tracing.pipeline_trace(
        seed=f"legalbench-{run_id}",
        session_id=f"legalbench-{run_id}",
        name=f"legalbench-{task_id}",
        input={"task": task_id, "model": model, "n": n},
        metadata=meta,
        tags=["mailroom", "legalbench", task_id],
        environment=_environment(),
    ) as root:
        yield root


@contextmanager
def question_observation(
    index: int,
    task_id: str,
    *,
    input_data: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Iterator[Any]:
    """One child observation per question/document (no-op when disabled)."""
    with tracing.observation(
        "answer-question",
        as_type="generation",
        input=input_data,
        metadata={"index": index, "task_id": task_id, **(metadata or {})},
    ) as span:
        yield span


def ensure_score_configs_if_enabled() -> None:
    """Register the legalbench_* score configs in Langfuse (idempotent)."""
    if tracing.is_enabled():
        ensure_score_configs()


def attach_run_scores(scores: dict[str, Any], task_id: str) -> None:
    """Attach run-level scores to the ACTIVE trace (call inside the block).

    Maps the suite's score dict to the registered legalbench_* score names;
    every call no-ops when tracing is disabled.
    """
    if not tracing.is_enabled():
        return
    score_trace("legalbench_task", task_id, data_type="TEXT")
    value = scores.get("accuracy")
    if isinstance(value, (int, float)):
        score_trace("legalbench_accuracy", float(value), data_type="NUMERIC")
    value = scores.get("macro_f1") or scores.get("macro_category_accuracy") or scores.get("yes_f1")
    if isinstance(value, (int, float)):
        score_trace("legalbench_macro_f1", float(value), data_type="NUMERIC")
    value = scores.get("calibration_error")
    if isinstance(value, (int, float)):
        score_trace("legalbench_calibration_error", float(value), data_type="NUMERIC")
    value = scores.get("n_questions") or scores.get("n_documents")
    if isinstance(value, (int, float)):
        score_trace("legalbench_n_questions", float(value), data_type="NUMERIC")
