"""Extraction attempt history and the opt-in "keep the best attempt" selection (#254).

``extract_node`` and ``retry_extract_node`` overwrite ``extracted_data``, so the
last attempt used to win even when the completeness judge rated it worse than an
earlier one. Two separate things live here:

* **Recording (always on).** Every attempt is appended to
  ``extraction_attempt_history``; ``judge_verify_node`` fills in the verdict of
  the attempt it judged (always the latest, it judges ``state["extracted_data"]``).
  The history rides on the state, so it is part of the pipeline result and of
  the manifest, and offline scoring can score each attempt.
* **Selection (opt-in).** With the taxonomy knob ``keep_best_attempt`` on
  (``routing.keep_best_attempt_enabled``, per class overridable, off by default
  until the judge is calibrated, #242), ``compile_report`` and the review
  handoff use the best-ranked attempt instead of the latest. Routing decisions
  are not changed: selection only decides which attempt's fields are kept.

Selection rule: only attempts that carry a judge verdict AND passed the
schema/quality gate are candidates; rank ``complete`` > ``partial`` >
``incomplete``, then higher ``extraction_confidence``, ties keep the LATEST. An
unjudged latest attempt is kept unless it failed the gate, because nothing says
it is worse.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

HISTORY_KEY = "extraction_attempt_history"

# complete > partial > incomplete. Anything else (skipped, judge_error, None) is "no verdict".
VERDICT_RANK = {"complete": 2, "partial": 1, "incomplete": 0}

# A parse failure keeps the model's raw reply in ``_raw``; the history keeps a stub of it.
RAW_REPLY_MAX_CHARS = 2000

# State fields restored when an earlier attempt is selected (the attempt's own, consistent set).
_RESTORED_FIELDS = (
    "extracted_data",
    "extraction_confidence",
    "extraction_guardrail",
    "extraction_gate",
    "judge_verdict",
    "judge_score",
    "judge_findings",
    "judge_source_verified",
)


def _bounded_extraction(extracted: Mapping[str, Any] | None) -> dict[str, Any]:
    """A deep copy of the extraction for the history, with a runaway ``_raw`` reply cut down."""
    copy = deepcopy(dict(extracted or {}))
    raw = copy.get("_raw")
    if isinstance(raw, str) and len(raw) > RAW_REPLY_MAX_CHARS:
        copy["_raw"] = raw[:RAW_REPLY_MAX_CHARS] + "...[truncated]"
    return copy


def _chunks_lost(extracted: Mapping[str, Any] | None) -> bool:
    value = (extracted or {}).get("_failed_chunks")
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def record_attempt(
    state: Mapping[str, Any],
    *,
    kind: str,
    extracted: Mapping[str, Any] | None,
    confidence: float | None,
    guard: Mapping[str, Any],
    gate: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The history with this attempt appended (a new list; the state is not mutated).

    ``attempt_no`` is the 1-based position in the history, so it stays unique
    even when a human-approved re-extraction restarts ``extraction_attempts``
    (that path also clears the history).
    """
    history = [dict(entry) for entry in state.get(HISTORY_KEY) or []]
    issues = list(guard.get("issues") or [])
    schema_valid = guard.get("schema_valid")
    entry: dict[str, Any] = {
        "attempt_no": len(history) + 1,
        "kind": kind,
        "extracted_data": _bounded_extraction(extracted),
        "extraction_confidence": confidence,
        "guardrail": issues,
        "schema_valid": schema_valid,
        # The schema/quality gate: guardrail clean (parse, schema, non-empty) and no lost window.
        "gate_ok": bool(guard.get("ok", not issues)) and not _chunks_lost(extracted),
        "judge_verdict": None,
        "judge_score": None,
        "judge_findings": None,
        "judge_source_verified": None,
    }
    if gate is not None:
        entry["extraction_gate"] = deepcopy(dict(gate))
    history.append(entry)
    return history


def record_verdict(
    state: Mapping[str, Any],
    *,
    verdict: str,
    score: float | None,
    findings: list[str] | None,
    source_verified: bool,
) -> list[dict[str, Any]] | None:
    """The history with the latest attempt's judge verdict filled in, or None when there is none.

    ``judge_verify_node`` judges ``state["extracted_data"]``, which is the latest
    attempt, so the verdict belongs to the last entry.
    """
    history = [dict(entry) for entry in state.get(HISTORY_KEY) or []]
    if not history:
        return None
    history[-1].update(
        judge_verdict=verdict,
        judge_score=score,
        judge_findings=list(findings or []),
        judge_source_verified=bool(source_verified),
    )
    return history


def _rank_key(entry: Mapping[str, Any]) -> tuple[int, float, int]:
    confidence = entry.get("extraction_confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else -1.0
    # attempt_no last: on a full tie the LATEST attempt wins (max).
    return (VERDICT_RANK[entry["judge_verdict"]], confidence, int(entry.get("attempt_no") or 0))


def _choose(history: list[Mapping[str, Any]], state: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    latest = history[-1]
    if len(history) == 1:
        return latest, "only_attempt"
    if state.get("conflict_detected"):
        # The Boss ruled on the latest attempt's values; archiving another attempt would bypass that ruling.
        return latest, "conflict_pins_latest"
    candidates = [
        e for e in history if e.get("judge_verdict") in VERDICT_RANK and e.get("gate_ok", True)
    ]
    latest_judged = latest.get("judge_verdict") in VERDICT_RANK
    if not latest_judged and latest.get("gate_ok", True):
        return latest, "latest_unjudged"
    if not candidates:
        return latest, "no_judged_attempt_passed_the_gate"
    best = max(candidates, key=_rank_key)
    if best is latest:
        return latest, "latest_ranks_best"
    return best, "better_judged_earlier_attempt"


def select_attempt(state: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    """State updates that select the attempt to report/review (``{}`` when there is nothing to select).

    Always records ``selected_attempt`` and ``selected_attempt_reason``. Only
    when ``enabled`` and an earlier attempt ranks higher are that attempt's own
    consistent fields restored (``extracted_data``, confidence, guardrail,
    judge verdict, score and findings). Idempotent: it reads only the history.
    Reviewer-supplied fields are never replaced.
    """
    history = [e for e in state.get(HISTORY_KEY) or [] if isinstance(e, Mapping)]
    if not history:
        return {}
    if state.get("extraction_source") == "human":
        return {}
    latest = history[-1]
    if not enabled:
        return {
            "selected_attempt": latest.get("attempt_no"),
            "selected_attempt_reason": "keep_best_attempt_off",
        }
    chosen, reason = _choose(history, state)
    updates: dict[str, Any] = {
        "selected_attempt": chosen.get("attempt_no"),
        "selected_attempt_reason": reason,
    }
    if chosen is not latest:
        for field in _RESTORED_FIELDS:
            source = "guardrail" if field == "extraction_guardrail" else field
            # Missing per-attempt evidence clears the latest's evidence too;
            # otherwise it could incorrectly certify a different extraction.
            updates[field] = deepcopy(chosen.get(source))
    return updates
