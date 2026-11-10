"""Node visit records collected in ``state["audit_trace"]`` (spec §4.4, §5.2).

``observability.tracing.traced_node`` builds one visit per node call with
:func:`visit` and returns it under ``audit_trace``; the :func:`append_visits`
reducer appends it, and LangGraph's checkpointer keeps the list across a review
pause or a crash. Terminal nodes (roadmap M7) copy the list into the row's
``detail.trace``. Nothing here writes to the audit DB.

A node that has more to record than its state fields (the old
``_emit_stage_audit`` events) calls :func:`record_visit` while it runs; the
note lands in that node's visit under ``decision["audit_event"]``.
"""

from __future__ import annotations

import contextvars
import os
import uuid
from typing import Any, TypedDict

#: §4.4 keys of every visit, in order. ``prompt`` and ``completion`` follow only when full_trace is on.
VISIT_KEYS: tuple[str, ...] = (
    "visit_id", "node", "attempt_id", "started_at", "ended_at", "status", "outcome", "model",
    "prompt_version", "prompt_tokens", "completion_tokens", "cost_usd", "latency_ms", "decisions",
    "decision", "trace_sink", "span_id", "also_in", "io_sha256",
)
FULL_TRACE_KEYS: tuple[str, ...] = ("prompt", "completion")
DECISION_KEYS: tuple[str, ...] = ("question", "type", "answer", "p", "expected", "options", "source")
DECISION_TYPES: tuple[str, ...] = ("noul", "choice", "score")
STATUSES: tuple[str, ...] = ("done", "error", "paused")
OUTCOMES: tuple[str, ...] = ("ok", "retry", "reroute", "fail")

_CLASSIFY = (
    "doc_type", "contract_subtype", "doc_subclass", "classification_confidence",
    "classification_attempts", "classification_guardrail",
)
_EXTRACT = (
    "extracted_data", "extraction_confidence", "extraction_attempts", "extraction_gate",
    "extraction_guardrail", "conflict_detected", "conflict_details",
)

#: Per graph node: the state fields copied (after the node ran) into the visit's ``decision``.
NODE_DECISION_FIELDS: dict[str, tuple[str, ...]] = {
    "intake": ("bert_triage", "intake_handoff", "file_sha256", "size_bytes", "intake_messy", "intake_changed"),
    "classify": _CLASSIFY,
    "retry_classify": _CLASSIFY,
    "review_classify": (
        "reviewer_doc_type", "reviewer_contract_subtype", "reviewer_doc_subclass", "reviewer_confidence",
        "review_verdict", "review_route", "reviewer_causes", "reviewer_model",
    ),
    "classification_arbiter": (
        "classification_arbiter_decision", "classification_arbiter_confidence",
        "classification_arbiter_reasoning", "doc_type", "contract_subtype", "doc_subclass",
    ),
    "extract": _EXTRACT,
    "retry_extract": _EXTRACT,
    "judge_verify": ("judge_verdict", "judge_score", "judge_findings", "judge_source_verified", "judge_pass_count"),
    "arbiter": ("arbiter_decision", "arbiter_reasoning", "arbiter_handoff", "arbiter_fields_to_fix", "arbiter_retry_count"),
    "human_review": ("escalation_reason", "review_causes", "review_decision"),
    "boss_escalation": ("doc_type", "boss_conflict_fingerprint", "conflict_details"),
    "compile_report": ("report_error",),
    "catalog_write": (),
    "archive": ("stage", "failure_class", "error_message"),
}

def decision_fields(node: str) -> tuple[str, ...]:
    """State fields recorded in a visit's ``decision``; the scoring family shares one list per role."""
    if node in NODE_DECISION_FIELDS:
        return NODE_DECISION_FIELDS[node]
    if node == "score_common":
        return ("scoring_common",)
    if node.startswith("score_"):
        return ("scoring", "extraction_adherence")
    return ()


#: Graph node -> attempt stage and the state counter that numbers its attempts.
_ATTEMPT_COUNTERS: dict[str, tuple[str, str]] = {
    "classify": ("classification", "classification_attempts"),
    "retry_classify": ("classification", "classification_attempts"),
    "review_classify": ("classification", "classification_attempts"),
    "classification_arbiter": ("classification", "classification_attempts"),
    "extract": ("extraction", "extraction_attempts"),
    "retry_extract": ("extraction", "extraction_attempts"),
    "judge_verify": ("judge", "judge_pass_count"),
    "arbiter": ("judge", "judge_pass_count"),
}


class LLMCallInfo(TypedDict, total=False):
    """What the LLM wrapper recorded for the calls one node made (``pipeline.limits.record_usage``)."""

    model: str | None
    prompt_version: str | int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    prompt: str | None
    completion: str | None


def append_visits(left: list | None, right: list | None) -> list:
    """Reducer for ``audit_trace``: append-only, never replaces earlier visits."""
    return [*(left or []), *(right or [])]


def full_trace_enabled() -> bool:
    """``MAILROOM_AUDIT_FULL_TRACE`` (wins when set) or taxonomy ``audit.full_trace``; default off."""
    raw = os.environ.get("MAILROOM_AUDIT_FULL_TRACE")
    if raw is not None and raw.strip():
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        from pipeline.config import load_config

        return bool(((load_config() or {}).get("audit") or {}).get("full_trace", False))
    except Exception:
        return False


def node_name(fn: Any) -> str:
    """Graph node name of a node function: ``classify_node`` -> ``classify``."""
    name = getattr(fn, "__name__", "") or "node"
    return name[: -len("_node")] if name.endswith("_node") else name


def attempt_id_for(state: dict, node: str) -> str | None:
    """Deterministic attempt id for a visit, or ``None`` for nodes outside an attempt stage.

    Built from ``doc_id``, the stage, ``run_attempt`` and the stage counter after
    the node ran; transient retries of the same call keep the same attempt id.
    """
    spec = _ATTEMPT_COUNTERS.get(node)
    if spec is None:
        return None
    stage, counter = spec
    n = state.get(counter) or 0
    if state.get("transient_error"):
        n += 1  # a transient failure does not consume the counter: it is the attempt that runs next
    run = state.get("run_attempt") or 0
    return f"att-{state.get('doc_id') or 'unknown'}-{stage}-r{run}-{n}"


def _expected_class(state: dict) -> Any:
    gt = state.get("ground_truth") or {}
    if not isinstance(gt, dict):
        return None
    return gt.get("expected_doc_class") or gt.get("expected_hf_class") or gt.get("expected")


def _doc_type_options() -> list[str] | None:
    try:
        from pipeline.config import get_all_doc_types

        return sorted(get_all_doc_types())
    except Exception:
        return None


def _decision(question: str, type_: str, answer: Any, p: Any, source: str, *, expected: Any = None,
              options: list | None = None) -> dict:
    return {"question": question, "type": type_, "answer": answer, "p": p, "expected": expected,
            "options": options, "source": source}


def decisions_for(node: str, state: dict, update: dict, *, event: dict | None = None) -> list[dict]:
    """The node's decisions in the sandbox schema ``{question, type, answer, p, expected, options, source}``.

    Only decisions the node actually made are listed (a ``None`` answer is skipped).
    """
    s = {**(state or {}), **(update or {})}
    out: list[dict] = []
    if node in ("classify", "retry_classify"):
        out.append(_decision("doc_type", "choice", s.get("doc_type"), s.get("classification_confidence"), node,
                             expected=_expected_class(s), options=_doc_type_options()))
    elif node == "review_classify":
        out.append(_decision("doc_type", "choice", s.get("reviewer_doc_type"), s.get("reviewer_confidence"), node,
                             expected=_expected_class(s), options=_doc_type_options()))
    elif node == "classification_arbiter":
        out.append(_decision("classification_arbiter_decision", "choice", s.get("classification_arbiter_decision"),
                             s.get("classification_arbiter_confidence"), node))
    elif node == "intake":
        bert = s.get("bert_triage") or {}
        if isinstance(bert, dict):
            out.append(_decision("doc_type", "choice", bert.get("doc_type"),
                                 bert.get("calibrated_confidence") or bert.get("confidence"), "bert_triage",
                                 expected=_expected_class(s), options=_doc_type_options()))
    elif node in ("extract", "retry_extract"):
        out.append(_decision("extraction_confidence", "score", s.get("extraction_confidence"), None, node))
    elif node == "judge_verify":
        out.append(_decision("judge_verdict", "choice", s.get("judge_verdict"), s.get("judge_score"), node))
        out.append(_decision("judge_score", "score", s.get("judge_score"), None, node))
    elif node == "arbiter":
        out.append(_decision("arbiter_decision", "choice", s.get("arbiter_decision"), None, node))
    elif node == "human_review":
        decision = s.get("review_decision")
        if isinstance(decision, dict):
            decision = decision.get("disposition") or decision.get("decision")
        out.append(_decision("review_decision", "choice", decision, None, node))
    elif node == "boss_escalation":
        detail = (event or {}).get("detail") or {}
        out.append(_decision("boss_decision", "choice", detail.get("decision"), None, node))
    return [d for d in out if d["answer"] is not None]


def _outcome(status: str, update: dict) -> str:
    if status == "error" or update.get("run_aborted") or update.get("failure_class"):
        return "fail"
    if update.get("transient_error"):
        return "retry"
    return "ok"


def visit(node: str, state: dict, update: dict, *, started_at, ended_at, status: str = "done",
          llm: LLMCallInfo | None = None, span_id: str | None = None, event: dict | None = None,
          full_trace: bool | None = None) -> dict:
    """Build one §4.4 visit record. Fields the run did not collect are ``None``."""
    from audit.sinks import record_io

    state = state or {}
    update = update if isinstance(update, dict) else {}
    merged = {**state, **update}
    llm = llm or {}
    visit_id = f"v-{uuid.uuid4().hex}"
    decision = {f: merged.get(f) for f in decision_fields(node)}
    if event:
        decision["audit_event"] = event
    decisions = decisions_for(node, state, update, event=event)
    sink, sha = None, None
    if llm.get("prompt") is not None or llm.get("completion") is not None:
        sink, sha = record_io(visit_id=visit_id, doc_id=merged.get("doc_id"), prompt=llm.get("prompt"),
                              completion=llm.get("completion"))
    record = {
        "visit_id": visit_id,
        "node": node,
        "attempt_id": attempt_id_for(merged, node),
        "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
        "ended_at": ended_at.isoformat() if hasattr(ended_at, "isoformat") else ended_at,
        "status": status,
        "outcome": _outcome(status, update),
        "model": llm.get("model"),
        "prompt_version": llm.get("prompt_version"),
        "prompt_tokens": llm.get("prompt_tokens"),
        "completion_tokens": llm.get("completion_tokens"),
        "cost_usd": llm.get("cost_usd"),
        "latency_ms": _latency_ms(started_at, ended_at),
        "decisions": decisions,
        "decision": decision,
        "trace_sink": sink,
        "span_id": span_id,
        "also_in": [],
        "io_sha256": sha,
    }
    if full_trace if full_trace is not None else full_trace_enabled():
        record["prompt"] = llm.get("prompt")
        record["completion"] = llm.get("completion")
    return record


def _latency_ms(started_at, ended_at) -> int | None:
    try:
        return int(round((ended_at - started_at).total_seconds() * 1000))
    except Exception:
        return None


# ------------------------------------------------------- notes from inside a node

_current_event: contextvars.ContextVar[dict | None] = contextvars.ContextVar("audit_visit_event", default=None)


def begin_visit() -> contextvars.Token:
    """Called by ``traced_node`` before the node runs; opens an empty note slot."""
    return _current_event.set({})


def end_visit(token: contextvars.Token) -> dict | None:
    """Called by ``traced_node`` after the node ran; returns the note (or ``None``) and closes the slot."""
    note = _current_event.get()
    _current_event.reset(token)
    return note or None


def record_visit(event: str, *, actor: str = "pipeline", detail: dict | None = None) -> None:
    """Attach an event to the running node's visit (``decision["audit_event"]``).

    Replaces the old per-stage audit rows: nothing is written to the audit DB.
    Outside a traced node the note is dropped.
    """
    slot = _current_event.get()
    if slot is None:
        return
    slot.clear()
    slot.update({"event": event, "actor": actor, "detail": dict(detail or {})})


def llm_info_since(start: int) -> LLMCallInfo | None:
    """Combine the LLM calls recorded since index ``start`` of the run's usage list into one ``LLMCallInfo``."""
    from pipeline.limits import call_cost_usd, run_calls

    calls = run_calls()[start:]
    if not calls:
        return None
    prompts = [c.get("prompt") for c in calls if c.get("prompt") is not None]
    completions = [c.get("completion") for c in calls if c.get("completion") is not None]
    last = calls[-1]
    costs = [call_cost_usd(c) for c in calls]
    return LLMCallInfo(
        model=last.get("served_model") or last.get("model"),
        prompt_version=last.get("prompt_version"),
        prompt_tokens=sum(c.get("prompt_tokens") or 0 for c in calls),
        completion_tokens=sum(c.get("completion_tokens") or 0 for c in calls),
        cost_usd=None if all(c is None for c in costs) else round(sum(c or 0.0 for c in costs), 6),
        prompt="\n\n".join(prompts) if prompts else None,
        completion="\n\n".join(completions) if completions else None,
    )


def run_calls_count() -> int:
    from pipeline.limits import run_calls

    return len(run_calls())


__all__ = [
    "DECISION_KEYS", "DECISION_TYPES", "FULL_TRACE_KEYS", "LLMCallInfo", "NODE_DECISION_FIELDS", "OUTCOMES",
    "STATUSES", "VISIT_KEYS", "append_visits", "attempt_id_for", "decision_fields", "begin_visit", "decisions_for", "end_visit",
    "full_trace_enabled", "llm_info_since", "node_name", "record_visit", "run_calls_count", "visit",
]
