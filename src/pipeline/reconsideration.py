"""Objective review / reconsideration causes.

Self-reported ``classification_confidence`` / ``extraction_confidence`` can
be overconfident. These triggers are derived only from grounded labels,
extraction payloads, schema/guardrail flags, and reporting completeness —
never from the model's stated confidence alone.

Canonical cause tokens stay in sync with The-Mailroom
``mailroom_ui/reconsideration.py`` (PR #14). Routing uses the same helpers
to park GT class misses on Lane A, retry hollow / low-coverage extracts,
and withhold ``catalog_write`` when ``compile_report`` fails.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

# Canonical cause tokens (keep in sync with The-Mailroom).
CLASS_MISS = "class_miss"
SUBCLASS_MISS = "subclass_miss"
EXTRACTION_MISS = "extraction_miss"
JUDGE_MISS = "judge_miss"
JUDGE_PARTIAL = "judge_partial"
SCHEMA_INVALID = "schema_invalid"
GUARDRAIL = "guardrail"
PARSE_ERROR = "parse_error"
REPORTING_INCOMPLETE = "reporting_incomplete"
NEEDS_JUDGE = "needs_judge_review"
HOLLOW_EXTRACTION = "hollow_extraction"

CAUSE_LABELS: dict[str, str] = {
    CLASS_MISS: "classification miss vs ground truth",
    SUBCLASS_MISS: "subclass miss vs ground truth",
    EXTRACTION_MISS: "extraction score below floor (not self-reported conf)",
    JUDGE_MISS: "judge verdict MISS",
    JUDGE_PARTIAL: "judge verdict PARTIAL — incomplete reporting",
    SCHEMA_INVALID: "schema invalid",
    GUARDRAIL: "guardrail triggered",
    PARSE_ERROR: "parse error",
    REPORTING_INCOMPLETE: "report / completeness incomplete",
    NEEDS_JUDGE: "deterministic scorer wanted a judge that the high-conf path skipped",
    HOLLOW_EXTRACTION: "hollow extraction payload (no public fields)",
}

_PUBLIC_SKIP = frozenset({"reasoning", "confidence", "mock_extraction"})
_TRUTHY_ZERO = frozenset({0, 0.0, "0", "false", "False", False})
_EMPTY = (None, "", [], {})


def align_class(token: Optional[str]) -> Optional[str]:
    """Canonical class for GT comparison (``merger_agreement`` ≡ ``contract``)."""
    if not token:
        return None
    key = str(token).strip().lower()
    if not key:
        return None
    from pipeline.config import EXTRACT_CLASS_ALIASES

    return EXTRACT_CLASS_ALIASES.get(key, key)


def expected_class(state: Mapping[str, Any] | None) -> Optional[str]:
    gt = (state or {}).get("ground_truth") or {}
    if not isinstance(gt, dict):
        return None
    return align_class(
        gt.get("expected_doc_class") or gt.get("expected_hf_class") or gt.get("expected")
    )


def predicted_class(state: Mapping[str, Any] | None, *, reviewer: bool = False) -> Optional[str]:
    data = state or {}
    if reviewer:
        token = data.get("reviewer_doc_type") or data.get("doc_type")
    else:
        token = data.get("doc_type")
    return align_class(token)


def class_misses_ground_truth(
    state: Mapping[str, Any] | None, *, reviewer: bool = False
) -> bool:
    """True when a grounded expected class disagrees with the predicted class."""
    want = expected_class(state)
    got = predicted_class(state, reviewer=reviewer)
    if not want or not got:
        return False
    return want != got


def extraction_is_hollow(extracted: Any) -> bool:
    """True when the payload has no public extraction fields.

    Missing ``extracted_data`` is not hollow — unit fixtures omit it to
    exercise the confidence arm. An empty dict or confidence-only stub is.
    """
    if extracted is None:
        return False
    if not isinstance(extracted, dict):
        return True
    for key, value in extracted.items():
        if str(key).startswith("_") or key in _PUBLIC_SKIP:
            continue
        if value not in _EMPTY:
            return False
    return True


def expected_field_coverage(
    extracted: Mapping[str, Any] | None,
    expected_fields: Mapping[str, Any] | None,
) -> float | None:
    """Fraction of non-empty expected fields that the extract surfaced."""
    if not expected_fields:
        return None
    required = {
        key: value for key, value in expected_fields.items() if value not in _EMPTY
    }
    if not required:
        return None
    payload = extracted if isinstance(extracted, dict) else {}
    present = 0
    for key in required:
        value = payload.get(key)
        if isinstance(value, list):
            ok = len(value) > 0
        else:
            ok = value not in _EMPTY
        present += int(ok)
    return round(present / len(required), 3)


def coverage_below_floor(
    extracted: Mapping[str, Any] | None,
    expected_fields: Mapping[str, Any] | None,
    *,
    floor: float | None = None,
) -> bool:
    """True when grounded field coverage is below the visualizer floor."""
    if floor is None:
        from pipeline.config import get_confidence_thresholds

        floor = float(get_confidence_thresholds().get("low", 0.70))
    coverage = expected_field_coverage(extracted, expected_fields)
    if coverage is None:
        return False
    return coverage < floor


def report_is_failed(state: Mapping[str, Any] | None) -> bool:
    """True when compile_report produced a fallback error report."""
    data = state or {}
    if data.get("report_error") is True:
        return True
    extracted = data.get("extracted_data")
    if not isinstance(extracted, dict):
        return False
    report = extracted.get("_report")
    return isinstance(report, dict) and report.get("error") is True


def _is_falsey_flag(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "off"}:
        return True
    return value in _TRUTHY_ZERO


def _is_true_flag(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "on", "1"}:
        return True
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return bool(value) and value not in _TRUTHY_ZERO


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collect_review_causes(
    state: Mapping[str, Any] | None = None,
    *,
    scores: Optional[Mapping[str, Any]] = None,
    verdict: Optional[str] = None,
) -> list[str]:
    """Canonical cause tokens from pipeline state and/or Langfuse scores."""
    data = dict(state or {})
    scores = dict(scores or {})
    causes: list[str] = []

    if class_misses_ground_truth(data):
        causes.append(CLASS_MISS)
    if _is_falsey_flag(scores.get("class_correct") or scores.get("classification_correct")):
        if CLASS_MISS not in causes:
            causes.append(CLASS_MISS)

    gt = data.get("ground_truth") or {}
    expected_sub = str((gt.get("expected_subclass") if isinstance(gt, dict) else "") or "").strip().lower()
    predicted_sub = str(data.get("doc_subclass") or data.get("contract_subtype") or "").strip().lower()
    if expected_sub and predicted_sub and expected_sub != predicted_sub:
        causes.append(SUBCLASS_MISS)

    raw_extracted = data.get("extracted_data")
    extracted = raw_extracted if isinstance(raw_extracted, dict) else {}
    expected_fields = gt.get("expected_fields") if isinstance(gt, dict) else None
    from pipeline.config import get_confidence_thresholds

    floor = float(get_confidence_thresholds().get("low", 0.70))
    overall = _as_float(scores.get("extraction_overall_score") or scores.get("extraction_field_score"))
    if overall is not None and overall < floor:
        causes.append(EXTRACTION_MISS)
    presence = _as_float(scores.get("expected_field_presence"))
    if presence is None:
        presence = expected_field_coverage(extracted, expected_fields)
    if presence is not None and presence < floor and EXTRACTION_MISS not in causes:
        causes.append(EXTRACTION_MISS)
    # Missing extracted_data is not hollow (classify-only park). An empty or
    # confidence-only dict on state is.
    if isinstance(raw_extracted, dict) and extraction_is_hollow(raw_extracted):
        causes.append(HOLLOW_EXTRACTION)

    verdict_token = (verdict or str(data.get("judge_verdict") or scores.get("mailroom-pipeline-judge") or "")).strip()
    upper = verdict_token.upper()
    if upper == "MISS":
        causes.append(JUDGE_MISS)
    elif upper == "PARTIAL":
        causes.append(JUDGE_PARTIAL)

    if _is_falsey_flag(scores.get("schema_valid")):
        causes.append(SCHEMA_INVALID)
    if _is_true_flag(scores.get("guardrail_triggered")):
        causes.append(GUARDRAIL)
    if _is_true_flag(scores.get("parse_error")):
        causes.append(PARSE_ERROR)
    if extracted.get("_parse_error"):
        if PARSE_ERROR not in causes:
            causes.append(PARSE_ERROR)

    completeness = _as_float(scores.get("completeness"))
    completeness_label = str(scores.get("completeness_label") or data.get("judge_verdict") or "").strip().upper()
    if (completeness is not None and completeness < floor) or completeness_label in {"LOW", "INCOMPLETE"}:
        causes.append(REPORTING_INCOMPLETE)
    if report_is_failed(data):
        if REPORTING_INCOMPLETE not in causes:
            causes.append(REPORTING_INCOMPLETE)

    if _is_true_flag(scores.get("extraction_needs_judge_review")):
        causes.append(NEEDS_JUDGE)

    seen: set[str] = set()
    ordered: list[str] = []
    for token in causes:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


def format_causes(causes: Sequence[str]) -> Optional[str]:
    if not causes:
        return None
    labels = [CAUSE_LABELS.get(c, c.replace("_", " ")) for c in causes]
    return "reconsider: " + "; ".join(labels)


def should_reconsider(stage: Optional[str], causes: Iterable[str]) -> bool:
    """True when a run looks finished but objective misses remain."""
    if not causes:
        return False
    st = (stage or "").strip().lower()
    return st in {"archived", "archive", "catalog", "report", "compile_report", "cataloged"}
