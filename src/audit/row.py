"""Build and write the one audit row a finished document gets (spec §4.1, §4.2, §5.3; roadmap M7).

``build_row(state, event=, actor=)`` turns the final graph state, including its
``audit_trace``, into a :class:`~audit.models.TerminalRow` whose ``detail`` has
exactly the §4.1 keys (``ROW_DETAIL_KEYS``). Fields a run did not collect are
``None``. ``attempts_from_trace`` cuts the trace into attempt sub-rows (§4.2).

``write_row`` is what terminal nodes call: it builds the row, commits it with
``audit.writer.commit_or_park`` (three tries, then the trace sinks' pending
store, §5.6), records the outcome on the manifest and never raises.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import structlog

from audit.models import AttemptRow, TerminalRow, WriteResult

logger = structlog.get_logger(__name__)

ROW_SCHEMA = 1

#: The §4.1 ``detail`` keys, in table order. ``recovered_from`` is added only by ``audit-reconcile``.
ROW_DETAIL_KEYS: tuple[str, ...] = (
    "row_schema",
    "stage", "pipeline_success", "failure_class", "error_message", "escalation_reason", "run_attempt",
    "review_decision", "arbiter_decision", "classification_arbiter_decision",
    "doc_type", "doc_subclass", "classification_confidence", "classification_attempts",
    "extraction",
    "judge_verdict", "judge_score", "judge_findings",
    "scoring", "metrics",
    "nodes_visited", "trace", "trace_mode", "trace_id", "trace_incomplete",
    "attempts", "refs",
)
EXTRACTION_KEYS: tuple[str, ...] = (
    "extracted_data", "extraction_source", "extraction_confidence", "extraction_attempts", "adherence",
    "extraction_gate", "extraction_guardrail", "specialist", "model", "prompt_version",
)
REF_KEYS: tuple[str, ...] = ("report_path", "archive_path", "manifest_path", "sidecar_path", "file_sha256")

#: Terminal event -> the stage the row records.
EVENT_STAGE = {"archived": "archived", "failed": "failed", "triage_archived": "archived", "triage_reviewed": "review"}
_SUCCESS_EVENTS = ("archived", "triage_archived")
_STAGE_OF_NODE = {
    "classify": "classification", "retry_classify": "classification", "review_classify": "classification",
    "classification_arbiter": "classification", "extract": "extraction", "retry_extract": "extraction",
    "judge_verify": "judge", "arbiter": "judge",
}
_EXTRACT_NODES = ("extract", "retry_extract")


# ---------------------------------------------------------------- attempts


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _sum(values) -> float | int | None:
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(vals) if vals else None


def _attempt_no(attempt_id: str) -> int:
    try:
        return int(attempt_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _trigger(group: list[dict], previous: dict | None, seen_stage: bool) -> str:
    first = group[0]
    if previous is not None and previous.get("node") == "human_review":
        return "resume"
    if previous is not None and previous.get("node") in ("arbiter", "classification_arbiter"):
        return "arbiter_rescue"
    if first.get("node") == "classification_arbiter":
        return "arbiter_rescue"
    if not seen_stage:
        return "initial"
    if first.get("node", "").startswith("retry_") or first.get("node") == "review_classify":
        return "retry"
    if any(v.get("outcome") == "retry" for v in group):
        return "transient"
    return "retry"


def _attempt_detail(stage: str, group: list[dict], state: Mapping[str, Any]) -> dict[str, Any]:
    last = group[-1].get("decision") or {}
    detail: dict[str, Any] = {
        "nodes": [v.get("node") for v in group],
        "visit_ids": [v.get("visit_id") for v in group],
        "decisions": [d for v in group for d in (v.get("decisions") or [])],
        "prompt_tokens": _sum(v.get("prompt_tokens") for v in group),
        "completion_tokens": _sum(v.get("completion_tokens") for v in group),
        "cost_usd": _sum(v.get("cost_usd") for v in group),
        "model": next((v.get("model") for v in reversed(group) if v.get("model")), None),
    }
    if stage == "classification":
        detail.update({
            "doc_type": last.get("doc_type", last.get("reviewer_doc_type")),
            "contract_subtype": last.get("contract_subtype", last.get("reviewer_contract_subtype")),
            "doc_subclass": last.get("doc_subclass", last.get("reviewer_doc_subclass")),
            "confidence": last.get("classification_confidence", last.get("reviewer_confidence")),
        })
    elif stage == "extraction":
        data = last.get("extracted_data")
        detail["extracted_data"] = data
        detail["extraction_confidence"] = last.get("extraction_confidence")
        detail.update(_attempt_scores(state, data, last))
    else:
        detail.update({k: last.get(k) for k in ("judge_verdict", "judge_score", "judge_findings",
                                                  "arbiter_decision", "arbiter_fields_to_fix")})
    return detail


def _attempt_scores(state: Mapping[str, Any], data: Any, last: Mapping[str, Any]) -> dict[str, Any]:
    """Score one extraction attempt when possible (§4.2).

    Compute adherence and, when ground truth is truthy, add scoring. On an
    error, log it and return ``adherence=None``.
    """
    try:
        from graph.scoring_nodes import adherence_for, score_document_block

        like = {**state, "extracted_data": data, "extraction_source": "model"}
        out: dict[str, Any] = {"adherence": adherence_for(like)}
        if state.get("ground_truth"):
            out["scoring"] = score_document_block(like, adherence=out["adherence"])["scoring"]
        return out
    except Exception:
        logger.warning("audit_attempt_scoring_failed", doc_id=state.get("doc_id"), exc_info=True)
        return {"adherence": None}


def attempts_from_trace(trace: list[dict], state: Mapping[str, Any] | None = None) -> list[AttemptRow]:
    """One sub-row per attempt id in the trace, in the order the attempts started (§4.2).

    Directly consecutive visits with the same ``attempt_id`` (a transient self-loop, or a
    judge pass and its arbiter) are one attempt. ``trigger`` comes from what ran
    before it: ``initial`` for a stage's first attempt, ``retry`` for a retry
    node, ``arbiter_rescue`` after an arbiter, ``resume`` after a human review.
    If an attempt id reappears later, the new sub-row gets a numeric suffix.
    """
    state = state or {}
    groups: list[tuple[str, list[dict], dict | None]] = []
    previous: dict | None = None
    for v in trace or []:
        aid = v.get("attempt_id")
        if not aid:
            previous = v
            continue
        if groups and groups[-1][0] == aid and previous is groups[-1][1][-1]:
            groups[-1][1].append(v)
        else:
            groups.append((aid, [v], previous))
        previous = v
    out: list[AttemptRow] = []
    seen: set[str] = set()
    used: set[str] = set()
    for aid, group, before in groups:
        stage = _STAGE_OF_NODE.get(group[0].get("node"), "")
        if stage not in ("classification", "extraction", "judge"):
            continue
        attempt_id = aid
        n = 2
        while attempt_id in used:  # the same attempt id met again later (e.g. after a resume)
            attempt_id = f"{aid}.{n}"
            n += 1
        used.add(attempt_id)
        outcome = group[-1].get("outcome")
        out.append(AttemptRow(
            attempt_id=attempt_id,
            stage=stage,
            attempt_no=_attempt_no(aid),
            trigger=_trigger(group, before, stage in seen),
            outcome=outcome,
            started_at=_ts(group[0].get("started_at")),
            ended_at=_ts(group[-1].get("ended_at")),
            detail=_attempt_detail(stage, group, state),
        ))
        seen.add(stage)
    return out


def human_correction_row(state: Mapping[str, Any], attempts: list[AttemptRow]) -> AttemptRow | None:
    """The ``human_correction`` extraction sub-row: the person's diff, who, when (spec §5.4).

    ``None`` unless a person changed the fields. The model attempts before it keep
    their own scores, so the model is never credited with the correction.
    """
    from graph.scoring_nodes import human_corrected, model_payload

    if not human_corrected(state):
        return None
    label = state.get("human_label") if isinstance(state.get("human_label"), Mapping) else {}
    snapshot = label.get("model_snapshot") if isinstance(label.get("model_snapshot"), Mapping) else {}
    before = snapshot.get("extracted_data") if isinstance(snapshot.get("extracted_data"), Mapping) else {}
    final = model_payload(state.get("extracted_data"))
    final = final if isinstance(final, Mapping) else {}
    changed = label.get("field_corrections")
    if not isinstance(changed, Mapping) or not changed:
        changed = {k: v for k, v in final.items() if before.get(k) != v}
    at = _ts(label.get("resolved_at"))
    numbers = [a.attempt_no for a in attempts if a.stage == "extraction"]
    return AttemptRow(
        attempt_id=f"att-{state.get('doc_id')}-extraction-human-1",
        stage="extraction",
        attempt_no=(max(numbers) if numbers else 0) + 1,
        trigger="human_correction",
        outcome="ok",
        started_at=at,
        ended_at=at,
        detail={
            "nodes": ["human_review"],
            "reviewer": label.get("reviewer"),
            "resolved_at": label.get("resolved_at"),
            "resolution": label.get("resolution"),
            "diff": {
                "changed": {k: {"before": before.get(k), "after": v} for k, v in changed.items()},
                "removed": list(label.get("removed_fields") or []),
            },
            "extracted_data": dict(final),
            "adherence": state.get("extraction_adherence"),
            "scoring": state.get("scoring"),
        },
    )


# ---------------------------------------------------------------- the row


def _last_visit(trace: list[dict], nodes: tuple[str, ...]) -> dict:
    return next((v for v in reversed(trace) if v.get("node") in nodes), {})


def _extraction(state: Mapping[str, Any], trace: list[dict]) -> dict[str, Any]:
    from graph.scoring_nodes import human_corrected, model_payload

    last = _last_visit(trace, _EXTRACT_NODES)
    event = (last.get("decision") or {}).get("audit_event") or {}
    data = state.get("extracted_data")
    return {
        "extracted_data": model_payload(data) if data is not None else None,
        # ``human`` only when a person changed the fields; an as-is approval is the model's (§5.4).
        "extraction_source": ("human" if human_corrected(state) else "model") if data is not None else None,
        "extraction_confidence": state.get("extraction_confidence"),
        "extraction_attempts": state.get("extraction_attempts"),
        "adherence": state.get("extraction_adherence"),
        "extraction_gate": state.get("extraction_gate"),
        "extraction_guardrail": state.get("extraction_guardrail"),
        "specialist": event.get("actor") if last else None,
        "model": last.get("model") if last else None,
        "prompt_version": last.get("prompt_version") if last else None,
    }


def row_metrics(state: Mapping[str, Any], *, stage: str) -> dict[str, Any]:
    """``emit_pipeline_scores`` output for the finished run (without attaching it), plus ``subclass_correct``.

    ``run_pipeline`` reuses this dict (``state["audit_metrics"]``) so the row and
    the emitted scores are the same numbers.
    """
    from graph.build_graph import _model_scoring_view
    from observability import scores as pipeline_scores

    view = {**_model_scoring_view(dict(state)), "stage": stage}
    from pipeline.limits import get_run_started

    started = get_run_started()
    started = started if isinstance(started, (int, float)) else time.time()
    metrics = pipeline_scores.compute_run_metrics(view, started, time.time())
    out = dict(pipeline_scores.compute_pipeline_scores(view, metrics))
    gt = state.get("ground_truth") if isinstance(state.get("ground_truth"), Mapping) else {}
    expected_sub = gt.get("expected_subclass")
    if expected_sub not in (None, ""):
        out["subclass_correct"] = int(str(state.get("doc_subclass") or "").strip().lower()
                                      == str(expected_sub).strip().lower())
    return out


def build_row(
    state: Mapping[str, Any],
    *,
    event: str,
    actor: str,
    refs: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    terminal_node: str | None = None,
) -> TerminalRow:
    """Build a terminal row with exactly ``ROW_DETAIL_KEYS`` in ``detail``.

    ``terminal_node`` is appended to ``nodes_visited`` when it is not already
    the last trace node; ``refs.file_sha256`` falls back to the state value
    when absent.
    """
    from audit.trace import full_trace_enabled

    trace = [dict(v) for v in (state.get("audit_trace") or []) if isinstance(v, Mapping)]
    nodes = [v.get("node") for v in trace]
    if terminal_node and (not nodes or nodes[-1] != terminal_node):
        nodes.append(terminal_node)  # the terminal node's own visit is appended after it returns
    stage = EVENT_STAGE.get(event, state.get("stage"))
    review = state.get("review_decision")
    detail: dict[str, Any] = {
        "row_schema": ROW_SCHEMA,
        "stage": stage,
        "pipeline_success": bool(event in _SUCCESS_EVENTS and not state.get("run_aborted") and not review),
        "failure_class": state.get("failure_class"),
        "error_message": state.get("error_message"),
        "escalation_reason": state.get("escalation_reason"),
        "run_attempt": state.get("run_attempt"),
        "review_decision": review,
        "arbiter_decision": state.get("arbiter_decision"),
        "classification_arbiter_decision": state.get("classification_arbiter_decision"),
        "doc_type": state.get("doc_type"),
        "doc_subclass": state.get("doc_subclass"),
        "classification_confidence": state.get("classification_confidence"),
        "classification_attempts": state.get("classification_attempts"),
        "extraction": _extraction(state, trace),
        "judge_verdict": state.get("judge_verdict"),
        "judge_score": state.get("judge_score"),
        "judge_findings": state.get("judge_findings"),
        "scoring": state.get("scoring"),
        "metrics": dict(metrics) if metrics is not None else None,
        "nodes_visited": nodes,
        "trace": trace,
        "trace_mode": "full" if full_trace_enabled() else "decisions",
        "trace_id": state.get("trace_id") or None,
        "trace_incomplete": bool(state.get("trace_incomplete")),
        "attempts": [],  # write_terminal_row fills in the sub-row hashes
        "refs": {k: (refs or {}).get(k) for k in REF_KEYS},
    }
    if detail["refs"]["file_sha256"] is None:
        detail["refs"]["file_sha256"] = state.get("file_sha256")
    return TerminalRow(
        doc_id=str(state.get("doc_id") or ""),
        matter_id=str(state.get("matter_id") or "DEFAULT"),
        event=event,
        actor=actor,
        detail=detail,
        attempts=_attempts(trace, state),
    )


def _attempts(trace: list[dict], state: Mapping[str, Any]) -> list[AttemptRow]:
    attempts = attempts_from_trace(trace, state)
    correction = human_correction_row(state, attempts)
    return attempts + [correction] if correction else attempts


def _run(coro_fn):
    """Run an async writer from synchronous code, using a thread if a loop is active."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro_fn()).result()


def write_row(
    state: Mapping[str, Any],
    *,
    event: str,
    actor: str,
    refs: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    terminal_node: str | None = None,
) -> WriteResult | None:
    """Build and commit a terminal row, or park it after write failures.

    Return the committed ``WriteResult``; return ``None`` when the row is
    skipped, cannot be built or written, or remains pending in a sink. These
    failures are logged so they do not abort the completed pipeline run.
    The manifest is marked with the commit or pending status when possible.
    """
    doc_id = state.get("doc_id")
    if not doc_id:
        logger.warning("audit_row_skipped_no_doc_id", event_name=event)
        return None
    try:
        row = build_row(state, event=event, actor=actor, refs=refs, metrics=metrics, terminal_node=terminal_node)
    except Exception:
        logger.exception("audit_row_build_failed", doc_id=doc_id, event_name=event)
        return None
    try:
        from audit.writer import commit_or_park

        outcome = _run(lambda: commit_or_park(row))
    except Exception:
        logger.exception("audit_row_write_failed", doc_id=doc_id, event_name=event)
        return None
    mark_manifest(str(doc_id), outcome.status, outcome.refs)
    return outcome.result


_AUDIT_WRITTEN = {"written": True, "pending": "pending", "pending_partial": "pending_partial", "gap": False}


def mark_manifest(doc_id: str, status: str, refs=()) -> None:
    """Record the row's write status and sink refs on an existing manifest.

    Missing manifests and load or save errors are ignored after logging the
    latter; this best-effort update never raises (§5.6 step 2).
    """
    try:
        from pipeline.bins import load_manifest, save_manifest

        manifest = load_manifest(doc_id)
        if manifest is None:
            return
        manifest.audit_written = _AUDIT_WRITTEN[status]
        manifest.audit_pending_refs = [dict(r) for r in refs]
        save_manifest(manifest)
    except Exception:
        logger.warning("audit_manifest_mark_failed", doc_id=doc_id, status=status, exc_info=True)


__all__ = [
    "EVENT_STAGE", "EXTRACTION_KEYS", "REF_KEYS", "ROW_DETAIL_KEYS", "ROW_SCHEMA", "attempts_from_trace",
    "human_correction_row",
    "build_row", "row_metrics", "write_row",
]
