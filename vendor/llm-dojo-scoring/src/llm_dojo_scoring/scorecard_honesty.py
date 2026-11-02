"""v0.18 scorecard honesty — fail-closed GT, metric identity, provenance, completion.

Live-run calibration for issues Exios66/llm-dojo-scoring #16–#22 (hub #233).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Mapping, Sequence

from . import __version__
from .gt_metadata import is_empty_value
from .maud import is_maud_class

__all__ = [
    "SCORER_VERSION",
    "PROVENANCE_KEYS",
    "COST_BASIS_TOKEN_PRICE",
    "COST_BASIS_VALUES",
    "IncomparableMetricsError",
    "UnknownMetricNameError",
    "ProvenanceExportError",
    "metric_id_for",
    "metric_ids_for_class",
    "metric_id_allowed",
    "assert_comparable_metric_ids",
    "assess_extraction_gt",
    "unscorable_extraction_result",
    "aggregate_quality_honest",
    "export_quality_verdict",
    "score_format_layer",
    "check_schema_promotion_gate",
    "DEFAULT_INSURANCE_SCHEMA_GATE",
    "DEFAULT_SCHEMA_GATE",
    "SCHEMA_PROMOTION_GATES",
    "classify_extraction_failure",
    "detect_maud_gt_ambiguity",
    "score_empty_field_contract",
    "canonical_error_class",
    "summarize_run_completion",
    "aggregate_quality_itt",
    "stamp_provenance",
    "validate_comparison_provenance",
    "normalize_serving_kind_token",
]

SCORER_VERSION = __version__

PROVENANCE_KEYS: frozenset[str] = frozenset(
    {
        "dataset_revision",
        "split",
        "draw_seed",
        "prompt_id",
        "metric_id",
        "scorer_version",
        "serving_kind",
        "model_id",
    }
)

#: USD computed as prompt/completion tokens × a provider price table
#: (``cost.estimate_cost``). An estimate, never comparable with a metered
#: GPU-time basis.
COST_BASIS_TOKEN_PRICE = "token_price_estimate"

COST_BASIS_VALUES: frozenset[str] = frozenset(
    {"busy_window", "billed_incl_cold", COST_BASIS_TOKEN_PRICE}
)

#: Canonical completion-error histogram keys (#21).
ERROR_CLASS_LENGTH_FINISH = "LengthFinish"
ERROR_CLASS_CONTEXT_OVERFLOW = "context_overflow"

_COLLAPSED_ANSWER_SPLIT = re.compile(r"\s+[\/|;]\s+")
_CAMEL_BOUNDARY = re.compile(r"([a-z])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")

# Registry metric name → canonical metric_id (issue #17).
_METRIC_ID_BY_NAME: dict[str, str] = {
    'sandbox_maud_accuracy': 'maud.sandbox_labeled.micro_accuracy',
    'sandbox_maud_clean_accuracy': 'maud.sandbox_clean_subset.micro_accuracy',
    'sandbox_maud_precision_answered': 'maud.sandbox_answered.precision',
    'sandbox_cuad_presence_f1': 'cuad.sandbox_category_detection.micro_f1',
    'sandbox_cuad_presence_precision': 'cuad.sandbox_category_detection.micro_precision',
    'sandbox_cuad_presence_recall': 'cuad.sandbox_category_detection.micro_recall',
    "extraction_overall_score": "pipeline.extraction.overall",
    "overall_score": "pipeline.extraction.overall",
    "extraction_f1": "pipeline.extraction.field_micro_f1",
    "extraction_f2": "pipeline.extraction.field_micro_f2",
    "extraction_precision": "pipeline.extraction.field_micro_precision",
    "extraction_recall": "pipeline.extraction.field_micro_recall",
    "extraction_category_presence": "cuad.clause_presence.micro_f1",
    "maud_question_accuracy": "maud.question.micro_accuracy",
    "maud_clause_presence": "maud.clause_presence.rate",
    "f1_macro": "pipeline.classification.f1_macro",
    "accuracy": "pipeline.classification.accuracy",
    "content_topic_accuracy": "pipeline.enron.topic_accuracy",
    "content_topic_f1_macro": "pipeline.enron.topic_f1_macro",
    "sentiment_accuracy": "pipeline.enron.sentiment_accuracy",
    "sentiment_f1_macro": "pipeline.enron.sentiment_f1_macro",
}

# Doc class / suite → allowed metric_ids for comparisons (documented in docs/METRIC_IDS.md).
# Covers all five live extract classes (#17): contract, merger_agreement,
# corporate_record, correspondence, insurance_claim.
_CLASS_METRIC_IDS: dict[str, tuple[str, ...]] = {
    "contract": (
        "pipeline.extraction.overall",
        "cuad.clause_presence.micro_f1",
        "pipeline.extraction.field_micro_f1",
        "pipeline.extraction.field_micro_f2",
        "pipeline.extraction.field_micro_precision",
        "pipeline.extraction.field_micro_recall",
        "maud.question.micro_accuracy",
        'cuad.sandbox_category_detection.micro_f1',
        'cuad.sandbox_category_detection.micro_precision',
        'cuad.sandbox_category_detection.micro_recall',
    ),
    "merger_agreement": (
        "pipeline.extraction.overall",
        "maud.question.micro_accuracy",
        "maud.clause_presence.rate",
        "pipeline.extraction.field_micro_f1",
        "pipeline.extraction.field_micro_f2",
        "pipeline.extraction.field_micro_precision",
        "pipeline.extraction.field_micro_recall",
        'maud.sandbox_labeled.micro_accuracy',
        'maud.sandbox_clean_subset.micro_accuracy',
        'maud.sandbox_answered.precision',
    ),
    "corporate_record": (
        "pipeline.extraction.overall",
        "pipeline.extraction.field_micro_f1",
        "pipeline.extraction.field_micro_f2",
        "pipeline.extraction.field_micro_precision",
        "pipeline.extraction.field_micro_recall",
    ),
    "insurance_claim": (
        "pipeline.extraction.overall",
        "pipeline.extraction.field_micro_f1",
        "pipeline.extraction.field_micro_f2",
        "pipeline.extraction.field_micro_precision",
        "pipeline.extraction.field_micro_recall",
    ),
    "correspondence": (
        "pipeline.extraction.overall",
        "pipeline.extraction.field_micro_f1",
        "pipeline.extraction.field_micro_f2",
        "pipeline.extraction.field_micro_precision",
        "pipeline.extraction.field_micro_recall",
        "pipeline.enron.topic_accuracy",
        "pipeline.enron.topic_f1_macro",
        "pipeline.enron.sentiment_accuracy",
        "pipeline.enron.sentiment_f1_macro",
    ),
}

#: Default ``schema_valid`` share a run must reach before promotion (#18).
DEFAULT_SCHEMA_GATE = 0.90
#: Kept for v0.18 callers; the gate is no longer insurance-only.
DEFAULT_INSURANCE_SCHEMA_GATE = DEFAULT_SCHEMA_GATE

#: Per-class promotion thresholds — every live extract class is gated (#18).
#: Override a class here (or pass ``threshold=``) rather than skipping it.
SCHEMA_PROMOTION_GATES: dict[str, float] = {
    "contract": DEFAULT_SCHEMA_GATE,
    "merger_agreement": DEFAULT_SCHEMA_GATE,
    "corporate_record": DEFAULT_SCHEMA_GATE,
    "correspondence": DEFAULT_SCHEMA_GATE,
    "insurance_claim": DEFAULT_SCHEMA_GATE,
}

_TRIAGE_GT_KEYS: frozenset[str] = frozenset(
    {
        "contract_subtype",
        "doc_type",
        "doc_subclass",
        "document_type",
        "confidence",
        "content_topic",
        "sentiment_label",
        "maud_clause_labels",
    }
)

_JSON_PROSE = re.compile(r"^\s*[\{\[]", re.MULTILINE)


class IncomparableMetricsError(ValueError):
    """Raised when two score legs use different metric_id values."""


class UnknownMetricNameError(KeyError):
    """Raised when a metric name has no canonical ``metric_id`` (#17)."""

    def __str__(self) -> str:  # KeyError repr-quotes its message by default
        """Return the error message without KeyError's added quotation marks."""
        return str(self.args[0]) if self.args else ""


class ProvenanceExportError(ValueError):
    """Raised when a comparison export lacks matching provenance stamps."""


@dataclass(frozen=True)
class GtAssessment:
    """Ground-truth scorability status and an optional reason for refusing a score."""

    status: str  # "scored" | "unscorable"
    reason: str | None = None

    @property
    def scorable(self) -> bool:
        """Return whether the assessment explicitly permits scoring."""
        return self.status == "scored"


def metric_id_for(
    metric_name: str,
    *,
    doc_class: str | None = None,
    presence_mode: bool = False,
) -> str:
    """Canonical ``metric_id`` for a registry metric name (#17).

    Strict: a name with no entry in the canonical table raises
    :class:`UnknownMetricNameError` rather than fabricating
    ``pipeline.<class>.<name>`` — a made-up id never matches another leg's,
    so it would hide an incomparable pair instead of refusing it.
    ``doc_class`` is accepted for call-site symmetry; the id does not depend
    on it. See ``docs/METRIC_IDS.md``.
    """
    try:
        canonical = _METRIC_ID_BY_NAME[metric_name]
    except KeyError:
        raise UnknownMetricNameError(
            f"no canonical metric_id for {metric_name!r}"
            + (f" (doc_class={doc_class!r})" if doc_class else "")
            + "; add it to _METRIC_ID_BY_NAME and docs/METRIC_IDS.md"
        ) from None
    if presence_mode:
        return "cuad.clause_presence.micro_f1"
    return canonical


def metric_ids_for_class(doc_class: str) -> tuple[str, ...]:
    """Return class-specific metric IDs, falling back to generic extraction."""
    return _CLASS_METRIC_IDS.get(doc_class, ("pipeline.extraction.overall",))


def metric_id_allowed(metric_id: str, doc_class: str) -> bool:
    """True when ``metric_id`` is on the class's allowed comparison list (#17).

    Unknown classes allow only ``pipeline.extraction.overall``.
    """
    return metric_id in metric_ids_for_class(doc_class)


def assert_comparable_metric_ids(
    left: str | None,
    right: str | None,
    *,
    hard: bool = True,
) -> dict[str, Any]:
    """Refuse silent cross-metric comparisons (#17).

    Both legs unset is legacy data with no identity to check: comparable,
    flagged ``metric_id_unverified``. One leg set and the other not is
    incomparable (``metric_id_missing``) — there is no shared identity.
    """
    if not left and not right:
        return {"comparable": True, "metric_id": None, "metric_id_unverified": True}
    if left == right:
        return {"comparable": True, "metric_id": left}
    reason = "metric_id_mismatch" if left and right else "metric_id_missing"
    detail = {
        "comparable": False,
        "incomparable": True,
        "incomparable_reason": reason,
        "left_metric_id": left,
        "right_metric_id": right,
    }
    if hard:
        raise IncomparableMetricsError(
            f"Cannot compare {left!r} with {right!r} ({reason}) — "
            "use the same metric_id on both legs."
        )
    return detail


def _is_empty_value(value: Any) -> bool:
    """Empty / null / stringified-empty (``"[]"``, ``"{}"``) Hub GT value."""
    return is_empty_value(value)


def _has_extractable_gt(
    expected: Mapping[str, Any],
    field_types: Mapping[str, str],
    scorable_gt_keys: Sequence[str] = (),
) -> bool:
    """Return whether GT has a nonempty extraction or suite-declared content value.

    Triage keys are ignored in ``field_types`` but may qualify when explicitly
    included in ``scorable_gt_keys``.
    """
    for key in field_types:
        if key in _TRIAGE_GT_KEYS:
            continue
        if not _is_empty_value(expected.get(key)):
            return True
    # Content/MAUD differentiators are scorable when the calling suite carries
    # the matching scorer (correspondence topic/sentiment, MAUD question
    # accuracy). They stay triage-only for suites without those scorers.
    for key in scorable_gt_keys:
        if not _is_empty_value(expected.get(key)):
            return True
    return False


def assess_extraction_gt(
    expected: Any,
    field_types: Mapping[str, str] | None,
    *,
    doc_class: str = "extraction",
    presence_expectations: Any = None,
    scorable_gt_keys: Sequence[str] | None = None,
) -> GtAssessment:
    """Fail-closed GT check (#16). Triage-only contracts GT is unscorable.

    ``scorable_gt_keys`` lets a suite declare content-only differentiators it
    can genuinely score (e.g. ``content_topic`` for correspondence,
    ``maud_clause_labels`` for the merger specialist). A GT row holding only
    those keys is scored on the content metric instead of being suppressed
    as ``gt_no_extractable_fields``. Contracts triage-only rows (subtype /
    doc_type labels with no scorer) remain unscorable.

    Return a ``GtAssessment`` with status and reason: missing or non-dict
    input is unscorable; an empty dict is scored with reason ``gt_empty``.
    For a nonempty dict, truthy ``presence_expectations`` permits scoring
    without extractable fields. This check does not compute any scores.
    """
    if expected is None:
        return GtAssessment("unscorable", "gt_missing")
    if not isinstance(expected, dict):
        return GtAssessment("unscorable", "gt_wrong_schema")
    if not expected:
        return GtAssessment("scored", "gt_empty")
    ftypes = dict(field_types or {})
    if presence_expectations:
        return GtAssessment("scored", None)
    if _has_extractable_gt(expected, ftypes, tuple(scorable_gt_keys or ())):
        return GtAssessment("scored", None)
    # Triage-only contract rows (subtype labels without schema fields).
    if doc_class in {"contract", "merger_agreement"} or any(
        k in expected for k in ("contract_subtype", "doc_type", "cuad_family")
    ):
        return GtAssessment("unscorable", "gt_no_extractable_fields")
    if not ftypes:
        return GtAssessment("unscorable", "gt_no_field_map")
    return GtAssessment("unscorable", "gt_no_extractable_fields")


def unscorable_extraction_result(
    assessment: GtAssessment,
    *,
    doc_class: str = "extraction",
    metric_id: str | None = None,
) -> dict[str, Any]:
    """Build an unscorable extraction payload with null scores and a GT reason."""
    mid = metric_id or metric_id_for("extraction_overall_score", doc_class=doc_class)
    return {
        "status": "unscorable",
        "reason": assessment.reason,
        "metric_id": mid,
        "overall_score": None,
        "extraction_overall_score": None,
        "quality_mean": None,
        "n_scored": 0,
        "n_unscorable": 1,
        "export_verdict": export_quality_verdict(assessment),
    }


def aggregate_quality_honest(
    doc_results: Sequence[Mapping[str, Any]],
    *,
    score_key: str = "overall_score",
    precision: int | None = 4,
) -> dict[str, Any]:
    """Quality mean excludes unscorable rows, retaining real zero (#16).

    The default keeps four-decimal fmean behavior. ``precision=None`` preserves
    legacy consumer sum/length arithmetic without rounding; population and
    empty-support policy remain canonical for either precision mode.
    """
    scored_vals: list[float] = []
    n_unscorable = 0
    for row in doc_results:
        if row.get("status") == "unscorable":
            n_unscorable += 1
            continue
        val = row.get(score_key)
        if val is None and isinstance(row.get("extraction"), dict):
            val = row["extraction"].get("overall_score")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            scored_vals.append(float(val))
    n_scored = len(scored_vals)
    return {
        "n_scored": n_scored,
        "n_unscorable": n_unscorable,
        "quality_mean": (
            (sum(scored_vals) / n_scored if precision is None else round(fmean(scored_vals), precision))
            if scored_vals else None
        ),
        "quality_population": "scored_only",
        # Nothing scored (empty batch or every row unscorable) proves nothing.
        "export_verdict": "unproven" if n_scored == 0 else "scored",
    }


def export_quality_verdict(assessment: GtAssessment | None) -> str:
    """Label unscorable assessments unproven and all other cases scored."""
    if assessment is None or assessment.scorable:
        return "scored"
    return "unproven"


def _try_parse_json(text: str) -> tuple[Any | None, bool]:
    """Parse JSON-looking text, returning its value and a success flag."""
    stripped = text.strip()
    if not stripped:
        return None, False
    if not (_JSON_PROSE.match(stripped) or stripped.startswith("{")):
        return None, False
    try:
        return json.loads(stripped), True
    except json.JSONDecodeError:
        return None, False


def score_format_layer(
    *,
    predicted: Any = None,
    predicted_raw: str | None = None,
    required_keys: Sequence[str] | None = None,
) -> dict[str, float]:
    """Separate parse/schema from field extraction (#18, #20).

    A batch (list/tuple of predictions) is scored item by item and the
    shares are averaged — a list is never itself "the parsed object", so
    missing keys and unparseable strings inside a batch count.
    """
    if predicted_raw is None and isinstance(predicted, (list, tuple)):
        if not predicted:
            return {"parse_ok": 0.0, "schema_valid": 0.0, "schema_adherence": 0.0}
        per_item = [
            score_format_layer(predicted=item, required_keys=required_keys)
            for item in predicted
        ]
        parse = round(fmean(r["parse_ok"] for r in per_item), 4)
        schema = round(fmean(r["schema_valid"] for r in per_item), 4)
        return {"parse_ok": parse, "schema_valid": schema, "schema_adherence": schema}
    parsed = predicted
    parse_ok = 1.0
    if predicted_raw is not None:
        parsed, ok = _try_parse_json(predicted_raw)
        parse_ok = 1.0 if ok else 0.0
    elif isinstance(predicted, str):
        parsed, ok = _try_parse_json(predicted)
        parse_ok = 1.0 if ok else 0.0
    elif predicted is None:
        parse_ok = 0.0
    schema_valid = parse_ok
    if parse_ok >= 1.0 and required_keys:
        if not isinstance(parsed, dict):
            schema_valid = 0.0
        else:
            missing = [k for k in required_keys if k not in parsed]
            if missing:
                schema_valid = round(1.0 - len(missing) / max(len(required_keys), 1), 4)
    return {
        "parse_ok": float(parse_ok),
        "schema_valid": float(schema_valid),
        "schema_adherence": float(schema_valid),
    }


def check_schema_promotion_gate(
    schema_valid: float | None,
    *,
    threshold: float | None = None,
    doc_class: str | None = None,
) -> dict[str, Any]:
    """Refuse promotion when structural validity is below the class gate (#18).

    A high overall score with invalid JSON must not read as PASS. The
    threshold is ``threshold`` if given, else the class entry in
    :data:`SCHEMA_PROMOTION_GATES`, else :data:`DEFAULT_SCHEMA_GATE`.
    A missing ``schema_valid`` fails closed.
    """
    if threshold is None:
        threshold = SCHEMA_PROMOTION_GATES.get(doc_class or "", DEFAULT_SCHEMA_GATE)
    out: dict[str, Any] = {"threshold": threshold}
    if doc_class:
        out["doc_class"] = doc_class
    if schema_valid is None:
        return {**out, "passes": False, "reason": "schema_valid_missing"}
    ok = float(schema_valid) >= threshold
    return {
        **out,
        "passes": ok,
        "schema_valid": float(schema_valid),
        "reason": None if ok else "schema_valid_below_threshold",
    }


def classify_extraction_failure(
    *,
    parse_ok: float | None = None,
    schema_valid: float | None = None,
    overall_score: float | None = None,
) -> str:
    """Format vs capability tagging (#20)."""
    if parse_ok is not None and parse_ok < 1.0:
        return "format_parse"
    if schema_valid is not None and schema_valid < 1.0:
        return "format_schema"
    if overall_score is not None and overall_score < 1.0:
        return "capability_miss"
    return "ok"


def score_empty_field_contract(
    expected: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    *,
    field_types: Mapping[str, str] | None = None,
    penalize_spurious_empty: bool = True,
) -> dict[str, Any]:
    """Correctly-empty → credit 1.0; spurious fill on empty GT → penalty (#20).

    Empty expected fields never enter :func:`score_extraction` ``overall_score``
    (archive mean of nonempty expected fields). This helper scores them
    separately so incorrectly-empty vs spurious-fill is explicit.
    """
    from .field_scoring import NEVER_SCORED_FIELDS, RETIRED_PROMPT_KEYS

    expected = dict(expected or {})
    predicted = dict(predicted or {})
    types = dict(field_types or {})
    field_scores: dict[str, float] = {}
    n_correctly_empty = 0
    n_spurious_fill = 0
    for name, exp_val in expected.items():
        if name in NEVER_SCORED_FIELDS or (
            name in RETIRED_PROMPT_KEYS and name not in types
        ):
            continue
        if types and name not in types:
            continue
        if not _is_empty_value(exp_val):
            continue
        if _is_empty_value(predicted.get(name)):
            field_scores[name] = 1.0
            n_correctly_empty += 1
            continue
        score = 0.0 if penalize_spurious_empty else 1.0
        field_scores[name] = score
        if penalize_spurious_empty:
            n_spurious_fill += 1
    n_empty = len(field_scores)
    credit = round(fmean(field_scores.values()), 4) if field_scores else None
    return {
        "field_scores": field_scores,
        "n_empty_expected": n_empty,
        "n_correctly_empty": n_correctly_empty,
        "n_spurious_fill": n_spurious_fill,
        "empty_field_credit": credit,
        "penalize_spurious_empty": penalize_spurious_empty,
    }


def _split_collapsed_answers(value: Any) -> list[str]:
    """Expand list answers and ``Yes / Strict liability`` collapsed strings (#19)."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_split_collapsed_answers(item))
        return out
    text = str(value).strip()
    if not text:
        return []
    parts = [p.strip() for p in _COLLAPSED_ANSWER_SPLIT.split(text) if p.strip()]
    if len(parts) >= 2:
        return parts
    return [text]


def detect_maud_gt_ambiguity(exp_labels: Mapping[str, Any]) -> set[str]:
    """Collapsed / multi-answer MAUD GT → ambiguous keys (#19).

    Distinct sub-question keys are left alone. A Hub key that holds several
    answers (list, slash-collapsed string, or an explicit ``gt_ambiguous``
    flag) is marked unscorable instead of matching one arbitrary value.
    """
    ambiguous: set[str] = set()
    for question, raw in exp_labels.items():
        answers: list[str] = []
        if isinstance(raw, dict):
            if raw.get("ambiguous") or raw.get("gt_ambiguous"):
                ambiguous.add(question)
                continue
            answer = raw.get("answer")
            # A whole catalog class is one answer even when it contains " / "
            # (four Tail Period classes read "... No-Vote / MTC Failure
            # Trigger"); splitting it made those GT rows unscorable.
            if isinstance(answer, str) and is_maud_class(
                question, answer, raw.get("valid_classes")
            ):
                continue
            answers = _split_collapsed_answers(answer)
        elif raw is not None:
            if isinstance(raw, str) and is_maud_class(question, raw):
                continue
            answers = _split_collapsed_answers(raw)
        normalized = {_fold(a) for a in answers if _fold(a)}
        if len(normalized) > 1:
            ambiguous.add(question)
    return ambiguous


def _fold(value: Any) -> str:
    """Lowercase a value and collapse non-ASCII-alphanumeric runs to spaces."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def canonical_error_class(token: str | None) -> str:
    """Histogram bucket for completion errors (#21)."""
    if token is None or str(token).strip() == "":
        return "unknown_error"
    raw = str(token).strip()
    spaced = _CAMEL_BOUNDARY.sub(r"\1 \2", raw)
    spaced = _ACRONYM_BOUNDARY.sub(r"\1 \2", spaced)
    folded = re.sub(r"[^a-z0-9]+", " ", spaced.lower()).strip()
    compact = folded.replace(" ", "")
    if (
        folded in {"length", "max tokens"}
        or "length finish" in folded
        or "lengthfinish" in compact
    ):
        return ERROR_CLASS_LENGTH_FINISH
    if any(
        needle in folded
        for needle in ("context overflow", "context window", "context length")
    ) or "contextoverflow" in compact:
        return ERROR_CLASS_CONTEXT_OVERFLOW
    return raw


def summarize_run_completion(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """LengthFinish / error honesty (#21)."""
    n_attempted = len(records)
    error_hist: dict[str, int] = {}
    n_errored = 0
    for rec in records:
        success_tokens = {
            "stop", "completed", "success", "none", "",
            "end_turn", "eos", "stop_sequence", "tool_calls",
        }
        # A success-like field cannot mask an independent failure signal.
        err = next((value for value in (
            rec.get("error"), rec.get("error_class"), rec.get("finish_reason")
        ) if value and str(value).strip().lower() not in success_tokens), None)
        status = str(rec.get("status") or "")
        is_error = bool(rec.get("errored") or status.upper().startswith("ERROR"))
        is_error = is_error or err is not None
        if not is_error:
            continue
        n_errored += 1
        key = canonical_error_class(err or status or "unknown_error")
        error_hist[key] = error_hist.get(key, 0) + 1
    n_completed = max(n_attempted - n_errored, 0)
    rate = round(n_completed / n_attempted, 4) if n_attempted else None
    return {
        "n_attempted": n_attempted,
        "n_completed": n_completed,
        "n_errored": n_errored,
        "completion_rate": rate,
        "error_class_histogram": error_hist,
    }


def aggregate_quality_itt(
    doc_results: Sequence[Mapping[str, Any]],
    completion: Mapping[str, Any],
    *,
    score_key: str = "overall_score",
) -> dict[str, Any]:
    """ITT population: attempted docs, errors count as zero quality (#21).

    Unscorable docs (#16) leave the denominator too: they have no ground
    truth, so they are neither a success nor a zero.
    """
    base = aggregate_quality_honest(doc_results, score_key=score_key)
    n_attempted = int(completion.get("n_attempted") or len(doc_results))
    n_attempted = max(n_attempted - base["n_unscorable"], base["n_scored"])
    mean = base.get("quality_mean")
    itt_mean = None
    if n_attempted and mean is not None:
        scored_total = float(mean) * base["n_scored"]
        itt_mean = round(scored_total / n_attempted, 4)
    out = dict(base)
    out.update(
        {
            "quality_population_itt": "attempted_including_errors_as_zero",
            "n_attempted_scorable": n_attempted,
            "quality_mean_itt": itt_mean,
            "quality_population_completed": "scored_only",
            "quality_mean_completed": mean,
        }
    )
    return out


def stamp_provenance(
    scorecard: Mapping[str, Any] | None = None,
    *,
    dataset_revision: str | None = None,
    split: str | None = None,
    draw_seed: str | None = None,
    prompt_id: str | None = None,
    metric_id: str | None = None,
    serving_kind: str | None = None,
    model_id: str | None = None,
    cost_basis: str | None = None,
) -> dict[str, Any]:
    """Mandatory provenance block (#22)."""
    base = dict(scorecard or {})
    prov = {
        "dataset_revision": dataset_revision or base.get("dataset_revision"),
        "split": split or base.get("split"),
        # 0 is a real draw seed; fall back only when it is unset.
        "draw_seed": next(
            (v for v in (draw_seed, base.get("draw_seed"), base.get("dataset_fingerprint"))
             if not _absent(v)),
            None,
        ),
        "prompt_id": prompt_id or base.get("prompt_id") or base.get("prompt_version"),
        "metric_id": metric_id or base.get("metric_id"),
        "scorer_version": base.get("scorer_version") or SCORER_VERSION,
        "serving_kind": normalize_serving_kind_token(
            serving_kind or base.get("serving_kind")
        ),
        "model_id": model_id or base.get("model_id") or base.get("model"),
    }
    if cost_basis or base.get("cost_basis"):
        prov["cost_basis"] = cost_basis or base.get("cost_basis")
    base["provenance"] = prov
    return base


def _absent(value: Any) -> bool:
    """Treat only None and blank strings as absent, preserving zero-valued seeds."""
    # 0 is a real draw seed; only None / "" count as missing.
    return value is None or (isinstance(value, str) and not value.strip())


#: Provenance keys a comparison may leave unset on *both* legs (a full-split
#: run has no draw seed). Set on one leg only is still refused.
_OPTIONAL_PAIRED_KEYS: frozenset[str] = frozenset({"draw_seed"})


def validate_comparison_provenance(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    require_cost_basis_if_cost: bool = True,
    vary: Sequence[str] = (),
) -> None:
    """Refuse exports when provenance keys differ (#22).

    Every key in ``PROVENANCE_KEYS`` must be present on both legs and equal,
    except:

    * keys named in ``vary`` — the axis the comparison is about (for
      local-vs-API serving, ``("serving_kind", "model_id")``) — which must be
      present on both legs but may differ;
    * ``draw_seed``, which may be absent on both legs (full split, no draw).

    ``cost_basis`` must be one of ``COST_BASIS_VALUES`` on any leg that
    carries one, and equal across legs when either leg carries cost.
    """
    if isinstance(vary, str):
        vary = (vary,)  # a bare key name, not its characters
    unknown_vary = sorted(set(vary) - PROVENANCE_KEYS)
    if unknown_vary:
        raise ValueError(f"vary names non-provenance keys: {unknown_vary}")
    lp = left.get("provenance") or left
    rp = right.get("provenance") or right
    missing: list[str] = []
    mismatched: list[str] = []
    for key in sorted(PROVENANCE_KEYS):
        lv, rv = lp.get(key), rp.get(key)
        if key in _OPTIONAL_PAIRED_KEYS and _absent(lv) and _absent(rv):
            continue
        if _absent(lv) or _absent(rv):
            missing.append(key)
        elif key in vary:
            continue
        elif str(lv) != str(rv):
            mismatched.append(key)
    if missing or mismatched:
        raise ProvenanceExportError(
            f"Comparison export refused: missing={missing} mismatched={mismatched}"
        )
    lb, rb = lp.get("cost_basis"), rp.get("cost_basis")
    invalid = [b for b in (lb, rb) if not _absent(b) and b not in COST_BASIS_VALUES]
    if invalid:
        raise ProvenanceExportError(
            f"Comparison export refused: cost_basis {invalid[0]!r} is not one of "
            f"{sorted(COST_BASIS_VALUES)}"
        )
    if not _absent(lb) and not _absent(rb) and lb != rb:
        # Never compare USD on two different bases, cost block or not.
        raise ProvenanceExportError(
            f"Comparison export refused: cost_basis mismatch ({lb!r} vs {rb!r})"
        )
    if require_cost_basis_if_cost and (left.get("cost") or right.get("cost")):
        if _absent(lb) or _absent(rb):
            raise ProvenanceExportError(
                "Comparison export refused: cost present but cost_basis missing"
            )


def normalize_serving_kind_token(kind: str | None) -> str | None:
    """Canonicalize serving-kind aliases while preserving empty inputs."""
    if not kind:
        return kind
    token = str(kind).strip().lower()
    if token in {"modal", "modal-vllm", "modal_vllm"}:
        return "modal"
    if token in {"local", "offline", "self-hosted", "on-prem"}:
        return "local"
    if token in {"api", "cloud", "hosted", "openrouter"}:
        return "api"
    return token
