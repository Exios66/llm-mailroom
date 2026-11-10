"""``audit_row_metrics``: one flat record per document from its unified row (spec §7).

The warehouse (``audit_rows_<date>``), ``audit-report``, ``GET /audit`` and the
scorecards read this instead of re-deriving numbers from the row. The latest
``rescore`` addendum supplies the scores; the latest ``resumed`` addendum
supplies the outcome. Model-only counterparts come from the last model
extraction attempt (not a ``human_correction`` sub-row), so a corrected
document still shows what the model itself scored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

#: The record's keys, in order (spec §7).
METRIC_KEYS: tuple[str, ...] = (
    "outcome", "pipeline_success", "first_pass", "doc_type", "doc_subclass", "class_correct", "subclass_correct",
    "overall_extraction_score", "suite_overall_extraction_score", "extraction_f1", "extraction_f2",
    "schema_adherence", "scoring_status",
    "model_overall_extraction_score", "model_suite_overall_extraction_score", "model_extraction_f1",
    "model_extraction_f2", "model_schema_adherence", "model_scoring_status",
    "classification_attempts", "extraction_attempts", "arbiter_rescued",
    "prompt_tokens", "completion_tokens", "cost_usd", "latency_ms",
    "reviewed", "human_corrected", "rescored", "resumed", "recovered",
)

_SUCCESS_EVENTS = ("archived", "triage_archived")


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _latest(addenda: Iterable[Mapping[str, Any]], kind: str) -> Mapping[str, Any] | None:
    found = [a for a in addenda if a.get("kind") == kind]
    return max(found, key=lambda a: a.get("seq") or 0) if found else None


def _num(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _flag(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _scores(scoring: Mapping[str, Any], adherence: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    headline, overall = _num(scoring.get("headline_score")), _num(scoring.get("overall_score"))
    return {
        f"{prefix}overall_extraction_score": headline if headline is not None else overall,
        f"{prefix}suite_overall_extraction_score": overall,
        f"{prefix}extraction_f1": _num(scoring.get("extraction_f1")),
        f"{prefix}extraction_f2": _num(scoring.get("extraction_f2")),
        f"{prefix}schema_adherence": _num(adherence.get("schema_adherence")),
        f"{prefix}scoring_status": scoring.get("status"),
    }


def _sum(values: Iterable[Any]) -> float | int | None:
    nums = [v for v in (_num(x) for x in values) if v is not None]
    return sum(nums) if nums else None


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None


def _latency_ms(detail: Mapping[str, Any]) -> float | None:
    seconds = _num(_d(detail.get("metrics")).get("run_duration_seconds"))
    if seconds is not None:
        return round(seconds * 1000.0, 1)
    trace = [v for v in detail.get("trace") or [] if isinstance(v, Mapping)]
    starts = [t for t in (_ts(v.get("started_at")) for v in trace) if t is not None]
    ends = [t for t in (_ts(v.get("ended_at")) for v in trace) if t is not None]
    if not starts or not ends:
        return None
    return round((max(ends) - min(starts)).total_seconds() * 1000.0, 1)


def audit_row_metrics(
    row: Mapping[str, Any], addenda: list[Mapping[str, Any]], attempts: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """The flat §7 record for one document. ``attempts`` are the row's and its addenda's sub-rows."""
    detail = _d(row.get("detail"))
    metrics = _d(detail.get("metrics"))
    extraction = _d(detail.get("extraction"))
    rescore = _latest(addenda, "rescore")
    resumed = _latest(addenda, "resumed")

    scoring = _d(detail.get("scoring"))
    adherence = _d(extraction.get("adherence"))
    if rescore is not None:
        scoring = _d(_d(rescore.get("detail")).get("scoring")) or scoring
        adherence = _d(_d(rescore.get("detail")).get("extraction_adherence")) or adherence

    outcome, success = row.get("event"), detail.get("pipeline_success")
    if resumed is not None:
        rdetail = _d(resumed.get("detail"))
        outcome = rdetail.get("event", outcome)
        success = rdetail.get("pipeline_success", outcome in _SUCCESS_EVENTS)

    ordered = sorted(attempts, key=lambda a: (str(a.get("ended_at") or a.get("started_at") or ""),
                                              a.get("attempt_no") or 0))
    model_attempts = [a for a in ordered if a.get("trigger") != "human_correction"]
    model_extraction = [a for a in model_attempts if a.get("stage") == "extraction"]
    last_model = _d(model_extraction[-1].get("detail")) if model_extraction else {}
    corrected = any(a.get("trigger") == "human_correction" for a in attempts) or (
        scoring.get("scored_payload") == "human_corrected")

    if model_extraction:
        model = _scores(_d(last_model.get("scoring")), _d(last_model.get("adherence")), "model_")
    elif not corrected:
        model = _scores(scoring, adherence, "model_")  # no sub-rows: the row's numbers are the model's
    else:
        model = _scores({}, {}, "model_")

    n_class = sum(1 for a in model_attempts if a.get("stage") == "classification")
    trace = [v for v in detail.get("trace") or [] if isinstance(v, Mapping)]
    out = {
        "outcome": outcome,
        "pipeline_success": _flag(success),
        "first_pass": _flag(metrics.get("success_rate")),
        "doc_type": detail.get("doc_type"),
        "doc_subclass": detail.get("doc_subclass"),
        "class_correct": _flag(metrics.get("class_correct")),
        "subclass_correct": _flag(metrics.get("subclass_correct")),
        **_scores(scoring, adherence),
        **model,
        "classification_attempts": n_class or _num(detail.get("classification_attempts")),
        "extraction_attempts": len(model_extraction) or _num(extraction.get("extraction_attempts")),
        "arbiter_rescued": any(a.get("trigger") == "arbiter_rescue" for a in attempts),
        "prompt_tokens": _sum(v.get("prompt_tokens") for v in trace),
        "completion_tokens": _sum(v.get("completion_tokens") for v in trace),
        "cost_usd": _sum(v.get("cost_usd") for v in trace),
        "latency_ms": _latency_ms(detail),
        "reviewed": detail.get("review_decision") is not None
        or any(a.get("kind") == "review_decision" for a in addenda),
        "human_corrected": corrected,
        "rescored": rescore is not None,
        "resumed": resumed is not None,
        "recovered": "recovered_from" in detail,
    }
    return out


def metrics_for(full_row: Mapping[str, Any]) -> dict[str, Any]:
    """``audit_row_metrics`` for a ``audit.writer.get_row`` result."""
    addenda = list(full_row.get("addenda") or [])
    attempts = list(full_row.get("sub_rows") or []) + [s for a in addenda for s in a.get("sub_rows") or []]
    return audit_row_metrics(full_row, addenda, attempts)


__all__ = ["METRIC_KEYS", "audit_row_metrics", "metrics_for"]
