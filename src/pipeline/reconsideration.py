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
# Decision-model reviewer causes (see ``pipeline.decision_review``).
SORTER_LOW_CONFIDENCE = "sorter_low_confidence"
REVIEWER_UNAVAILABLE = "reviewer_unavailable"
REVIEWER_NO_TEXT = "reviewer_no_text"
REVIEWER_INELIGIBLE_LABEL = "reviewer_ineligible_label"
REVIEWER_DISAGREEMENT = "reviewer_disagreement"
REVIEWER_LOW_CONFIDENCE = "reviewer_low_confidence"
# State-derived causes: every route into ``human_review`` carries at least one of
# these (or a legacy token above), even in live runs with no ground truth and no
# Langfuse scores. Mirror new tokens in The-Mailroom ``mailroom_ui/reconsideration.py``.
TRANSIENT_EXHAUSTED = "transient_exhausted"
BOSS_CONFLICT = "boss_conflict"
ARBITER_ESCALATED = "arbiter_escalated"
CLASS_ARBITER_ESCALATED = "classification_arbiter_escalated"
UNKNOWN_CLASS = "unknown_class"
EMPTY_DOCUMENT = "empty_document"
UNSUPPORTED_EXTRACTION = "unsupported_extraction"
LOW_EXTRACTION_CONFIDENCE = "low_extraction_confidence"
JUDGE_INCOMPLETE = "judge_incomplete"
JUDGE_ERROR = "judge_error"
RETRIES_EXHAUSTED = "retries_exhausted"
CHUNK_FAILED = "chunk_failed"

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
    SORTER_LOW_CONFIDENCE: "sorter confidence below the low threshold",
    REVIEWER_UNAVAILABLE: "decision reviewer returned no valid answer",
    REVIEWER_NO_TEXT: "document has no usable text for the decision reviewer",
    REVIEWER_INELIGIBLE_LABEL: "sorter or reviewer label is not an eligible class",
    REVIEWER_DISAGREEMENT: "sorter and decision reviewer labels disagree",
    REVIEWER_LOW_CONFIDENCE: (
        "decision reviewer confidence missing or below the class cutoff, "
        "or no valid cutoff for the class"
    ),
    TRANSIENT_EXHAUSTED: "provider errors outlasted the transient retry budget",
    BOSS_CONFLICT: "conflict with the matter record that the boss did not clear",
    ARBITER_ESCALATED: "arbiter escalated the extraction to a person",
    CLASS_ARBITER_ESCALATED: "classification arbiter did not accept a class",
    UNKNOWN_CLASS: "document class is unknown or not extractable",
    EMPTY_DOCUMENT: "document has no readable text",
    UNSUPPORTED_EXTRACTION: "no specialist extractor for this class",
    LOW_EXTRACTION_CONFIDENCE: "extraction confidence below the low band after retries",
    JUDGE_INCOMPLETE: "judge verdict INCOMPLETE — extraction misses the source",
    JUDGE_ERROR: "judge verification failed",
    RETRIES_EXHAUSTED: "confidence retry budget spent without a settled answer",
    CHUNK_FAILED: "one or more extraction windows failed",
}

_PUBLIC_SKIP = frozenset({"reasoning", "confidence", "mock_extraction"})
# Dojo / Hub eval extras — scored by specialist suites but not in the
# extraction schema or specialist prompts. Must not gate routing coverage.
_EVAL_EXTRA_GT_KEYS = frozenset({"content_topic", "sentiment_label", "maud_clause_labels"})
_TRUTHY_ZERO = frozenset({0, 0.0, "0", "false", "False", False})
_EMPTY = (None, "", [], {})


def align_class(token: Optional[str]) -> Optional[str]:
    """Canonical class for GT comparison. ``merger_agreement`` (MAUD) is
    not equivalent to ``contract`` (CUAD). Extract aliases, if any, still
    collapse for comparison.
    """
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


def routing_coverage_expected_fields(
    expected_fields: Mapping[str, Any] | None,
    doc_type: str | None = None,
    *,
    sources: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Expected-field subset used by the extraction coverage routing gate.

    Aligns with dojo ``peel_non_extraction_fields``: only registered schema
    fields count, eval extras (``content_topic``, ``sentiment_label``, …) are
    excluded, and post-hoc GT fills (regex-derived labels) do not park live
    runs when the document never stated the field.
    """
    if not expected_fields:
        return None
    from observability.specialist_suites import schema_fields

    allowed = frozenset(schema_fields(doc_type))
    if not allowed:
        allowed = frozenset(
            key
            for key, value in expected_fields.items()
            if value not in _EMPTY and key not in _EVAL_EXTRA_GT_KEYS
        )
    src = sources or {}
    filtered: dict[str, Any] = {}
    for key, value in expected_fields.items():
        if value in _EMPTY:
            continue
        if key in _EVAL_EXTRA_GT_KEYS:
            continue
        if allowed and key not in allowed:
            continue
        if str(src.get(key) or "").lower() == "posthoc":
            continue
        filtered[key] = value
    return filtered


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


def routed_field_coverage(
    extracted: Mapping[str, Any] | None,
    expected_fields: Mapping[str, Any] | None,
    *,
    doc_type: str | None = None,
    sources: Mapping[str, str] | None = None,
) -> float | None:
    """Coverage over the routing scope; None when no grounded field is in scope.

    CUAD/MAUD ground truth holds clause labels and no schema fields, so it
    scopes to nothing. That is an absent signal, never a coverage failure.
    """
    scoped = routing_coverage_expected_fields(
        expected_fields, doc_type, sources=sources
    )
    return expected_field_coverage(extracted, scoped)


def coverage_below_floor(
    extracted: Mapping[str, Any] | None,
    expected_fields: Mapping[str, Any] | None,
    *,
    doc_type: str | None = None,
    sources: Mapping[str, str] | None = None,
    floor: float | None = None,
) -> bool:
    """True when grounded field coverage is below the visualizer floor."""
    if floor is None:
        from pipeline.config import get_confidence_thresholds

        floor = float(get_confidence_thresholds().get("low", 0.70))
    coverage = routed_field_coverage(
        extracted, expected_fields, doc_type=doc_type, sources=sources
    )
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


_ACCEPTED_CLASS_ARBITER = frozenset({"accept_sorter", "accept_reviewer"})


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _transient_exhausted(data: Mapping[str, Any]) -> bool:
    """A transient flag still set at park time means the per-node budget ran out.

    Nodes clear ``transient_error`` on success, and routing retries the same
    node while its counter is within budget, so a set flag here is exhaustion.
    """
    return _is_true_flag(data.get("transient_error"))


def _failed_chunks(extracted: Mapping[str, Any]) -> int:
    value = extracted.get("_failed_chunks")
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return _as_int(value)


def _state_derived_causes(
    data: Mapping[str, Any], extracted: Mapping[str, Any], known: set[str]
) -> list[str]:
    """Causes read from the routing state itself (no ground truth, no scores).

    Routing functions in ``graph/routing.py`` return only a destination, so each
    park reason is rebuilt here from the same state signals they read. ``known``
    holds the tokens already collected so retry-exhaustion can build on them.
    """
    from pipeline.config import get_confidence_thresholds, is_extractable_doc_type

    out: list[str] = []

    def add(token: str) -> None:
        if token not in known and token not in out:
            out.append(token)

    thresholds = get_confidence_thresholds(
        data.get("doc_type") if isinstance(data.get("doc_type"), str) else None
    )
    low = float(thresholds.get("low", 0.70))
    high = float(thresholds.get("high", 0.95))
    retry_max = _as_int(thresholds.get("retry_max", 2))

    if _transient_exhausted(data):
        add(TRANSIENT_EXHAUSTED)

    if "doc_text" in data and not str(data.get("doc_text") or "").strip():
        add(EMPTY_DOCUMENT)

    doc_type = data.get("doc_type")
    if "doc_type" in data and not is_extractable_doc_type(
        doc_type if isinstance(doc_type, str) else None
    ):
        add(UNKNOWN_CLASS)

    # Classification budget spent without the sorter (or its reviewer) settling.
    sorter_conf = _as_float(data.get("classification_confidence"))
    class_attempts = _as_int(data.get("classification_attempts"))
    pre_extraction = not data.get("extracted_data") and not _as_int(
        data.get("extraction_attempts")
    )
    reviewer_accepted = data.get("review_route") == "extract" or data.get(
        "classification_arbiter_decision"
    ) in _ACCEPTED_CLASS_ARBITER
    if (
        pre_extraction
        and not reviewer_accepted
        and "classification_attempts" in data
        and class_attempts > retry_max
        and (sorter_conf is None or sorter_conf < high)
        and not _transient_exhausted(data)
        and UNKNOWN_CLASS not in out
        and UNKNOWN_CLASS not in known
    ):
        add(RETRIES_EXHAUSTED)

    # LLM-lane reviewer (no decision route): the verdict names why it parked.
    if data.get("review_route") is None:
        verdict = str(data.get("review_verdict") or "")
        if verdict == "reviewer_error":
            add(REVIEWER_UNAVAILABLE)
        elif verdict == "reviewer_agrees_low":
            add(REVIEWER_LOW_CONFIDENCE)
        elif verdict == "reviewer_conflicts":
            add(REVIEWER_DISAGREEMENT)
        elif verdict == "escalated":
            add(REVIEWER_LOW_CONFIDENCE)
        elif verdict in {"reviewer_overrides", "reviewer_agrees_high"}:
            if not is_extractable_doc_type(
                data.get("reviewer_doc_type")
                if isinstance(data.get("reviewer_doc_type"), str)
                else None
            ):
                add(REVIEWER_INELIGIBLE_LABEL)
            reviewer_conf = _as_float(data.get("reviewer_confidence"))
            if reviewer_conf is None or reviewer_conf < high:
                add(REVIEWER_LOW_CONFIDENCE)

    classification_arbiter = data.get("classification_arbiter_decision")
    if classification_arbiter and classification_arbiter not in _ACCEPTED_CLASS_ARBITER:
        add(CLASS_ARBITER_ESCALATED)

    stub = bool(extracted.get("_unsupported")) and all(
        str(k).startswith("_") or k == "reasoning" for k in extracted
    )
    if stub:
        add(UNSUPPORTED_EXTRACTION)

    # ``review`` is the boss's own "hand it to a person" ruling.
    if data.get("review_decision") == "review" or (
        _is_true_flag(data.get("conflict_detected")) and data.get("review_decision") != "approved"
    ):
        add(BOSS_CONFLICT)

    if data.get("extraction_guardrail"):
        add(GUARDRAIL)

    if _failed_chunks(extracted) > 0:
        add(CHUNK_FAILED)

    attempts = _as_int(data.get("extraction_attempts"))
    schema_probe = (
        isinstance(data.get("extracted_data"), dict)
        and bool(extracted)
        and not stub
        and isinstance(doc_type, str)
        and is_extractable_doc_type(doc_type)
        and not extracted.get("_parse_error")
    )
    retry_driven = False
    if schema_probe:
        try:
            from observability.scores import validate_extraction

            if validate_extraction(doc_type, dict(extracted)).get("schema_valid") is False:
                add(SCHEMA_INVALID)
                retry_driven = attempts > retry_max
        except Exception:  # noqa: BLE001 - schema probe must never break parking
            pass
    if not stub and ("extraction_attempts" in data or data.get("extracted_data")):
        ext_conf = _as_float(data.get("extraction_confidence"))
        judged = bool(data.get("judge_verdict")) or _as_int(data.get("judge_pass_count")) > 0
        unsure = (ext_conf is None or ext_conf < low) and not data.get("judge_source_verified")
        if unsure and (attempts > retry_max or judged):
            add(LOW_EXTRACTION_CONFIDENCE)
            retry_driven = retry_driven or attempts > retry_max
    if (HOLLOW_EXTRACTION in known or SCHEMA_INVALID in known) and attempts > retry_max:
        retry_driven = True
    if retry_driven:
        add(RETRIES_EXHAUSTED)

    # Lane B budgets.
    max_passes = _as_int(thresholds.get("judge_max_passes")) or 3
    judge_verdict = str(data.get("judge_verdict") or "")
    if judge_verdict in {"partial", "incomplete"} and _as_int(
        data.get("judge_pass_count")
    ) >= max_passes:
        add(RETRIES_EXHAUSTED)

    arbiter = data.get("arbiter_decision")
    if arbiter and arbiter != "accept_with_caveats":
        arbiter_max = _as_int(thresholds.get("arbiter_retry_max", 2))
        if arbiter != "retry_extraction":
            add(ARBITER_ESCALATED)
        elif _as_int(data.get("arbiter_retry_count")) > arbiter_max:
            add(ARBITER_ESCALATED)
            add(RETRIES_EXHAUSTED)

    return out


def collect_review_causes(
    state: Mapping[str, Any] | None = None,
    *,
    scores: Optional[Mapping[str, Any]] = None,
    verdict: Optional[str] = None,
) -> list[str]:
    """Return unique cause tokens from pipeline state and/or Langfuse scores.

    Nonempty string reviewer causes from a state list or tuple are included;
    duplicate tokens retain their first position.
    """
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
        sources = gt.get("expected_fields_sources") if isinstance(gt, dict) else None
        presence = routed_field_coverage(
            extracted,
            expected_fields,
            doc_type=data.get("doc_type"),
            sources=sources if isinstance(sources, dict) else None,
        )
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
    elif upper == "INCOMPLETE":
        causes.append(JUDGE_INCOMPLETE)
    elif upper == "JUDGE_ERROR":
        causes.append(JUDGE_ERROR)

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
    # Only the reporter's own label counts here. The judge's ``incomplete``
    # verdict is JUDGE_INCOMPLETE above, not a report failure.
    completeness_label = str(scores.get("completeness_label") or "").strip().upper()
    if (completeness is not None and completeness < floor) or completeness_label in {"LOW", "INCOMPLETE"}:
        causes.append(REPORTING_INCOMPLETE)
    if report_is_failed(data):
        if REPORTING_INCOMPLETE not in causes:
            causes.append(REPORTING_INCOMPLETE)

    if _is_true_flag(scores.get("extraction_needs_judge_review")):
        causes.append(NEEDS_JUDGE)

    sorter_confidence = _as_float(data.get("classification_confidence"))
    sorter_floor = float(get_confidence_thresholds(data.get("doc_type")).get("low", 0.70))
    if not causes and sorter_confidence is not None and sorter_confidence < sorter_floor:
        causes.append(SORTER_LOW_CONFIDENCE)

    causes.extend(_state_derived_causes(data, extracted, set(causes)))

    # Decision-model reviewer causes recorded by review_classify_node.
    reviewer_causes = data.get("reviewer_causes")
    if isinstance(reviewer_causes, (list, tuple)):
        causes.extend(t for t in reviewer_causes if isinstance(t, str) and t)

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
