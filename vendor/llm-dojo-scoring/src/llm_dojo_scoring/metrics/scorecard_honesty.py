"""v0.18 scorecard honesty — fail-closed GT, metric identity, provenance, completion.

Live-run calibration for issues Exios66/llm-dojo-scoring #16–#22 (hub #233).
"""

from __future__ import annotations

import json
import math
import numbers
import re
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Mapping, Sequence

from .. import __version__
from ..schemas.gt_metadata import is_empty_value
from ..schemas.maud import is_maud_class

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
    "headline_scope",
    "intent_split",
    "MAUD_HEADLINE_FIELDS",
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
    # Reviewer lane (issue #97 follow-on): one identity per measure, not per model.
    "reviewer_class_accuracy": "review.lane.class_accuracy",
    "reviewer_class_macro_accuracy": "review.lane.macro_accuracy",
    "reviewer_accepted_accuracy": "review.lane.accepted_accuracy",
    "reviewer_wrong_accept_rate": "review.lane.wrong_accept_rate",
    "reviewer_wrong_accept_upper_95": "review.lane.wrong_accept_upper_95",
    "reviewer_routed_to_human_review_rate": "review.lane.human_review_rate",
    "reviewer_accept_rule_violations": "review.lane.accept_rule_violations",
    "reviewer_subclass_accuracy": "review.lane.subclass_accuracy",
    "reviewer_verdict_accuracy": "review.lane.verdict_accuracy",
    "reviewer_confidence_ece": "review.lane.confidence_ece",
    "reviewer_confidence_brier": "review.lane.confidence_brier",
    "reviewer_cost_per_accepted_usd": "review.lane.cost_per_accepted_usd",
}

# Doc class / suite → allowed metric_ids for comparisons (documented in docs/contracts/METRIC_IDS.md).
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
    on it. See ``docs/contracts/METRIC_IDS.md``.
    """
    try:
        canonical = _METRIC_ID_BY_NAME[metric_name]
    except KeyError:
        raise UnknownMetricNameError(
            f"no canonical metric_id for {metric_name!r}"
            + (f" (doc_class={doc_class!r})" if doc_class else "")
            + "; add it to _METRIC_ID_BY_NAME and docs/contracts/METRIC_IDS.md"
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


#: Merger fields that carry MAUD answers. A merger headline whose scored
#: fields are all in this set is MAUD accuracy, not a field-level score (#95).
MAUD_HEADLINE_FIELDS: frozenset[str] = frozenset({"maud_clauses"})


def _row_field_scores(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The per-field scores a document row carries, wherever it nests them.

    The single lookup shared by the honest aggregate, :func:`intent_split` and
    the grid. Holders are checked in order: the row, ``row["extraction"]``,
    ``row["scores"]``. An empty ``field_scores`` mapping is skipped, so an
    empty top-level dict does not hide nested data.
    """
    for holder in (row, row.get("extraction"), row.get("scores")):
        if not isinstance(holder, Mapping):
            continue
        fields = holder.get("field_scores")
        if isinstance(fields, Mapping) and fields:
            return fields
    return None


def _is_real_score(value: Any) -> bool:
    """True for a finite real number that is not a bool (a field that was scored)."""
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _numeric_field_names(fields: Mapping[str, Any]) -> list[str]:
    """Names of the fields whose score is a real number (not ``None``, not a bool)."""
    return [str(name) for name, value in fields.items() if _is_real_score(value)]


#: Keys a class accepts for parse compatibility but never scores (0.28.0).
_PARSE_COMPAT_FIELDS: dict[str, frozenset[str]] = {
    "contract": frozenset({"merger_consideration", "maud_clauses"}),
}


def _live_fields(doc_class: str | None) -> tuple[str | None, list[str]]:
    """Resolve *doc_class* (stripped, lower-cased; specialist names accepted).

    Returns ``(native_class, live_fields)``; ``(None, [])`` when the class is
    not in ``DEFAULT_FIELD_TYPES`` and is not a ``SPECIALIST_DOC_TYPES`` name.
    """
    from ..scoring.suites import DEFAULT_FIELD_TYPES, SPECIALIST_DOC_TYPES

    if doc_class is None:
        return None, []
    name = str(doc_class).strip().lower()
    if name not in DEFAULT_FIELD_TYPES:
        name = SPECIALIST_DOC_TYPES.get(name, "")
    if name not in DEFAULT_FIELD_TYPES:
        return None, []
    # Parse-compat keys (the contract's v1 merger_consideration / maud_clauses) are
    # accepted in a prediction but never scored, so they never count toward scope.
    compat = _PARSE_COMPAT_FIELDS.get(name, frozenset())
    return name, [f for f in DEFAULT_FIELD_TYPES[name] if f not in compat]


def _is_maud_only_doc(scored_fields: Sequence[str], live: set[str]) -> bool:
    """True when a document scored at least one live field and all are MAUD fields."""
    scored_live = {f for f in scored_fields if f in live}
    return bool(scored_live) and scored_live <= MAUD_HEADLINE_FIELDS


def _normalise_counts(field_counts: Any) -> dict[str, int]:
    """Field → positive document count; unusable counts are skipped, not fatal."""
    if isinstance(field_counts, (str, bytes)):
        raise TypeError(
            "field_counts must be a mapping of field -> count or a collection of "
            f"field names, not {type(field_counts).__name__}"
        )
    if isinstance(field_counts, Mapping):
        counts: dict[str, int] = {}
        for name, n in field_counts.items():
            if isinstance(n, bool) or not isinstance(n, (int, float)):
                continue
            if isinstance(n, float) and not math.isfinite(n):
                continue
            if int(n) >= 1:
                counts[str(name)] = int(n)
        return counts
    return {str(name): 1 for name in field_counts}


def headline_scope(
    doc_class: str | None,
    field_counts: Mapping[str, int] | Sequence[str],
    *,
    n_docs: int | None = None,
    maud_only_docs: int | None = None,
    n_docs_with_fields: int | None = None,
) -> dict[str, Any]:
    """Which live fields a class headline actually covers (#95, #94).

    ``field_counts`` maps each scored field to the number of documents that
    scored it (or is a plain collection of scored fields). A ``str`` raises
    ``TypeError``; counts that are not positive ints/floats are skipped. The
    live field map comes from ``DEFAULT_FIELD_TYPES``; *doc_class* is stripped
    and lower-cased, and a ``SPECIALIST_DOC_TYPES`` name is accepted.
    ``headline_scope`` is:

    - ``"maud_only"`` — a merger headline built from MAUD answers alone; its
      ``headline_label`` says so, because it is MAUD accuracy, not a field mean;
    - ``"mixed"`` — a merger headline where only some documents are MAUD-only
      (the field union alone would read as ``"full"``); needs *n_docs* and
      *maud_only_docs*;
    - ``"partial"`` — some live fields never carried ground truth, so the
      headline is not comparable with a class scored on its whole field map;
    - ``"full"`` — every live field was scored at least once;
    - ``"unknown"`` — the caller has field data but no claim can be made: the
      class is unresolved, some scored documents lack field data, or no live
      field was scored. The label says why. Unresolved classes need scored
      fields to trigger this; the other two need *n_docs*;
    - ``None`` — no scored field is in the live map and the caller gave no
      per-document context (nothing scored, or only off-map fields).

    Per-document context (all optional; ``None`` keeps the field-union rule):
    *n_docs* is the number of scored documents, *n_docs_with_fields* how many
    of them carried field data, and *maud_only_docs* how many documents scored
    at least one live field and only MAUD ones (``MAUD_HEADLINE_FIELDS``).

    Scored fields outside the live map are listed in ``off_map_fields`` and
    kept in ``scored_fields``, but never count toward the scope,
    ``field_coverage`` or ``unscored_fields``.
    """
    resolved, live = _live_fields(doc_class)
    counts = _normalise_counts(field_counts)
    scored = sorted(counts)
    live_set = set(live)
    scored_live = [field for field in scored if field in live_set]
    unscored = [field for field in live if field not in set(scored_live)]
    out: dict[str, Any] = {
        "headline_scope": None,
        "headline_label": None,
        "scored_fields": {field: counts[field] for field in scored},
        "unscored_fields": unscored,
        "off_map_fields": [field for field in scored if field not in live_set],
        "field_coverage": (round(len(scored_live) / len(live), 6) if live else None),
    }
    reason: str | None = None
    if n_docs and n_docs_with_fields is not None and n_docs_with_fields < n_docs:
        reason = f"field data for {n_docs_with_fields} of {n_docs} scored documents"
    elif resolved is None and (scored or n_docs):
        reason = "document class unresolved"
    elif not scored_live and n_docs:
        reason = "no scored field in the live field map"
    if reason is not None:
        out.update(
            headline_scope="unknown",
            headline_label=reason,
            unscored_fields=None,
            field_coverage=None,
        )
        return out
    if not scored_live:
        return out
    k = None
    if resolved == "merger_agreement" and n_docs and maud_only_docs is not None:
        k = min(max(int(maud_only_docs), 0), int(n_docs))
    if resolved == "merger_agreement" and (
        k == n_docs if k is not None else set(scored_live) <= MAUD_HEADLINE_FIELDS
    ):
        out["headline_scope"] = "maud_only"
        out["headline_label"] = "MAUD answers only (maud_clauses); not a field-level score"
    elif k:
        out["headline_scope"] = "mixed"
        out["headline_label"] = (
            f"{k} of {n_docs} documents MAUD answers only; not a field-level score"
        )
    elif unscored:
        out["headline_scope"] = "partial"
        out["headline_label"] = (
            f"{len(live) - len(unscored)} of {len(live)} fields carry GT "
            f"({', '.join(f for f in live if f not in set(unscored))})"
        )
    else:
        out["headline_scope"] = "full"
        out["headline_label"] = f"all {len(live)} fields"
    return out


def intent_split(
    doc_results: Sequence[Mapping[str, Any]],
    *,
    field: str = "intent",
    hit_threshold: float = 0.5,
) -> dict[str, Any] | None:
    """Report ``intent`` apart from the other fields (#94).

    With three scored correspondence fields, ``intent`` is a third of every
    document score, so the class headline is bimodal: a missed intent caps a
    document near ⅔ and a hit floors it near ⅓. Over scorable rows that carry
    ``field_scores``, this returns:

    - ``intent_mean`` and ``n_intent`` — the mean of the intent field score;
    - ``n_intent_hit`` / ``n_intent_missed`` — split at ``hit_threshold``;
    - ``other_fields_mean`` and ``n_other`` — the mean of each document's
      mean over its other scored fields.

    Returns ``None`` when no row scored ``field``.

    Field scores are expected in ``[0, 1]``. Non-finite values (``nan``,
    ``inf``) are skipped for both the intent field and the other fields, so
    ``n_intent_hit + n_intent_missed == n_intent``.

    Standalone, this skips only rows marked ``status == "unscorable"``. It does
    not check that a row has a numeric headline score; :func:`aggregate_quality_honest`
    therefore passes it only the rows that actually entered the headline mean.
    """
    intents: list[float] = []
    others: list[float] = []
    for row in doc_results:
        if row.get("status") == "unscorable":
            continue
        fields = _row_field_scores(row)
        if not fields:
            continue
        value = fields.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            intents.append(float(value))
        rest = [
            float(v)
            for k, v in fields.items()
            if k != field
            and isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
        ]
        if rest:
            others.append(fmean(rest))
    if not intents:
        return None
    return {
        "field": field,
        "n_intent": len(intents),
        "intent_mean": round(fmean(intents), 4),
        "n_intent_hit": sum(1 for v in intents if v >= hit_threshold),
        "n_intent_missed": sum(1 for v in intents if v < hit_threshold),
        "n_other": len(others),
        "other_fields_mean": round(fmean(others), 4) if others else None,
    }


def aggregate_quality_honest(
    doc_results: Sequence[Mapping[str, Any]],
    *,
    score_key: str = "overall_score",
    precision: int | None = 4,
    doc_class: str | None = None,
) -> dict[str, Any]:
    """Quality mean excludes unscorable rows, retaining real zero (#16).

    The default keeps four-decimal fmean behavior. ``precision=None`` preserves
    legacy consumer sum/length arithmetic without rounding; population and
    empty-support policy remain canonical for either precision mode.

    When ``doc_class`` is given and rows carry ``field_scores``, the result
    also reports the scored population (:func:`headline_scope`): which live
    fields carried GT, in how many documents, and whether the headline covers
    the whole field map (#95; per-document, so a row mixing MAUD-only and
    full-field documents is ``"mixed"``, and a row where some scored documents
    lack field data is ``"unknown"``), and, when rows score ``intent``, an
    ``intent_split`` (:func:`intent_split`) so a bimodal intent-driven
    headline is visible (#94).
    """
    scored_vals: list[float] = []
    scored_rows: list[Mapping[str, Any]] = []
    n_unscorable = 0
    field_counts: dict[str, int] = {}
    doc_fields: list[list[str]] = []
    saw_fields = False
    for row in doc_results:
        if row.get("status") == "unscorable":
            n_unscorable += 1
            continue
        val = row.get(score_key)
        if val is None and isinstance(row.get("extraction"), dict):
            val = row["extraction"].get("overall_score")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            scored_vals.append(float(val))
            scored_rows.append(row)
            fields = _row_field_scores(row)
            if fields is not None:
                saw_fields = True
                doc_fields.append(_numeric_field_names(fields))
    n_scored = len(scored_vals)
    population: dict[str, Any] = {}
    if doc_class is not None and saw_fields:
        live = set(_live_fields(doc_class)[1])
        for names in doc_fields:
            for field in set(names):
                field_counts[field] = field_counts.get(field, 0) + 1
        population = headline_scope(
            doc_class,
            field_counts,
            n_docs=n_scored,
            n_docs_with_fields=len(doc_fields),
            maud_only_docs=sum(1 for names in doc_fields if _is_maud_only_doc(names, live)),
        )
        split = intent_split(scored_rows)
        if split is not None:
            population["intent_split"] = split
    return {
        **population,
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
    from ..scoring.field_scoring import NEVER_SCORED_FIELDS, RETIRED_PROMPT_KEYS

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
