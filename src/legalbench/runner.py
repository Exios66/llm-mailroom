"""LegalBench run orchestrator.

One run = one task, one deterministic sample, one model, one Langfuse trace,
one experiment-log record. The runner is task-agnostic: everything task
specific comes from the task registry (loader, call shape, extractor, scorer).
"""

from __future__ import annotations

import datetime as _dt
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Optional

from . import langfuse_tracing as _tracing
from . import prompts as _prompts
from .agent import LegalBenchAgent
from .experiment_log import append_record, build_record, regenerate
from .mock import MockLegalBenchModel
from .tasks import get_task

DEFAULT_MODEL = "qwen/qwen3.7-flash"


@dataclass
class RunResult:
    task_id: str
    kind: str
    model: str
    rows: list[dict[str, Any]]
    results: list[dict[str, Any]]
    scores: dict[str, Any]
    tokens: dict[str, Any]
    run_id: str
    record: dict[str, Any] = field(default_factory=dict)
    log_path: Optional[str] = None
    regenerated: dict[str, Any] = field(default_factory=dict)


def _tokens_summary(rows_with_usage: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = completion = total = rows = 0
    cost_values: list[float] = []
    for usage in rows_with_usage:
        if not isinstance(usage, dict) or not usage:
            continue
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        total += int(usage.get("total_tokens") or 0)
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            cost_values.append(float(cost))
        rows += 1
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost_usd": round(sum(cost_values) / len(cost_values), 6) if cost_values else 0.0,
        "cost_total_usd": round(sum(cost_values), 6),
        "rows_with_usage": rows,
    }


def _model_name(model: Any, fallback: str) -> str:
    if isinstance(model, str):
        return model
    return getattr(model, "model", fallback)


def run_task(
    task_id: str,
    *,
    n: int = 30,
    seed: int = 42,
    model: Any = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    mock: bool = False,
    trace_enabled: bool = True,
    rows: Optional[list[dict[str, Any]]] = None,
) -> RunResult:
    """Run one LegalBench task and (unless disabled) log it.

    ``model`` is a model name (real run), or any object implementing the
    agent interface (tests/--mock inject fakes).
    """
    task = get_task(task_id)
    rows = rows if rows is not None else task.loader(n, seed)
    if not rows:
        raise ValueError(f"task {task_id!r} produced no samples (n={n}, seed={seed})")

    if isinstance(model, str):
        if mock:
            agent: Any = MockLegalBenchModel(task.classes, seed=seed)
            model_label = MockLegalBenchModel.model
        else:
            system_prompt = _prompts.get_prompt(task.prompt_version, task.classes)
            agent = LegalBenchAgent(
                system_prompt,
                classes=task.classes,
                model=model or None,
                api_key=api_key,
            )
            model_label = model or DEFAULT_MODEL
    else:
        agent = model
        model_label = _model_name(model, DEFAULT_MODEL)

    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    run_id = f"legalbench-{task.id}-{seed}-{stamp}"
    if trace_enabled:
        try:
            from observability.tracing import ensure_process_tracing

            ensure_process_tracing()
        except Exception:
            pass
    results: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []

    if trace_enabled:
        _tracing.ensure_score_configs_if_enabled()
        trace_ctx = _tracing.legalbench_trace(
            run_id, task.id, model=model_label, n=len(rows)
        )
    else:
        trace_ctx = nullcontext()

    with trace_ctx:
        for i, row in enumerate(rows):
            expected = row.get("answer") or row.get("family") or ""
            obs_input = {
                "task": task.id,
                "expected": expected,
                "question": row.get("question"),
                "document": row.get("contract_title") or row.get("title"),
            }
            try:
                with _tracing.question_observation(
                    i, task.id, input_data=obs_input, metadata={"run_id": run_id}
                ):
                    prediction = task.call(agent, row)
                predicted, conf, support = task.extract(row, prediction)
                parse_error = isinstance(prediction, dict) and bool(prediction.get("_parse_error"))
                if parse_error or predicted == "error":
                    raise ValueError("unparseable model output")
                results.append(
                    {
                        "filename": row.get("contract_title") or row.get("title"),
                        "status": "ok",
                        "expected": expected,
                        "predicted": predicted,
                        "correct": predicted == expected,
                        "confidence": conf,
                        "support": support,
                        "cost_usd": None,
                        "error": None,
                        **{k: v for k, v in row.items() if k not in ("text", "document_text")},
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "filename": row.get("contract_title") or row.get("title"),
                        "status": "error",
                        "expected": expected,
                        "predicted": None,
                        "correct": False,
                        "confidence": None,
                        "support": "",
                        "cost_usd": None,
                        "error": str(exc)[:300],
                    }
                )
            usage = agent.usage() if hasattr(agent, "usage") else {}
            usages.append(usage)
            cost = usage.get("cost")
            if results and isinstance(cost, (int, float)):
                results[-1]["cost_usd"] = round(float(cost), 6)

        scores = task.scorer(results)
        if trace_enabled:
            # Scores must land on the active trace (score_current_trace).
            _tracing.attach_run_scores(scores, task.id)

    if trace_enabled:
        from observability import tracing

        tracing.flush()

    tokens = _tokens_summary(usages)
    record = build_record(
        task_id=task.id,
        kind=task.kind,
        experiment_name=f"{model_label.split('/')[-1]}_{task.prompt_version}",
        prompt_version=task.prompt_version,
        model=model_label,
        rows=rows,
        n_requested=n,
        seed=seed,
        scores=scores,
        results=results,
        tokens=tokens,
        parameters={"mock": mock, "model": model_label},
        data_source={
            "project": task.data_source_label,
            "ground_truth": task.ground_truth_label,
        },
    )
    return RunResult(
        task_id=task.id,
        kind=task.kind,
        model=model_label,
        rows=rows,
        results=results,
        scores=scores,
        tokens=tokens,
        run_id=run_id,
        record=record,
    )


def log_run(result: RunResult, jsonl_path: Optional[str] = None) -> dict[str, Any]:
    """Append the run's record + regenerate the log/site/docs (no-op safe)."""
    path = append_record(result.record, jsonl_path)
    touched = regenerate(path)
    result.log_path = str(path)
    result.regenerated = touched
    return touched


def print_summary(result: RunResult) -> None:
    scores = result.scores
    task = get_task(result.task_id)
    print()
    print(f"LEGALBENCH RUN — {task.label}")
    print(f"  task:      {result.task_id} ({result.kind})")
    print(f"  model:     {result.model}")
    print(f"  samples:   {len(result.rows)} (ok {result.scores.get('n_questions') or result.scores.get('n_documents') or 0}, "
          f"errors {result.scores.get('n_error') or 0})")
    for key in (
        "accuracy",
        "accuracy_equiv",
        "macro_f1",
        "macro_category_accuracy",
        "yes_f1",
        "confidence_mean",
        "calibration_error",
    ):
        value = scores.get(key)
        if value is not None:
            print(f"  {key:<22} {value}")
    print(f"  tokens:    {result.tokens.get('total_tokens', 0)} "
          f"(cost ${result.tokens.get('cost_total_usd', 0.0):.6f})")
    print(f"  trace:     legalbench-{result.run_id}" if os.environ.get("LANGFUSE_SECRET_KEY") else
          f"  trace:     (observability disabled — set LANGFUSE keys to trace)")
    if result.log_path:
        print(f"  log:       {result.log_path}")
        for label, path in result.regenerated.items():
            if path:
                print(f"  {label}: {path}")
