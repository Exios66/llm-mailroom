"""Canonical Mailroom score definitions. Dojo owns metric rules and registry."""
from __future__ import annotations
import math
from types import MappingProxyType

SCORE_CONFIGS: list[dict] = [
    {"name": "class_correct", "data_type": "BOOLEAN"},
    {"name": "stage_correct", "data_type": "BOOLEAN"},
    {"name": "parse_error", "data_type": "BOOLEAN"},
    {"name": "schema_valid", "data_type": "BOOLEAN"},
    {"name": "stage_completed", "data_type": "BOOLEAN"},
    # Production STP (straight-through processing). Dojo registry: gt=none,
    # "value is whatever the pipeline emits". Mailroom emits 1 only when the
    # document archived in one pass with no retry/Lane A/arbiter/boss/human
    # review/guardrail/transient self-loop — no ground truth required.
    {"name": "success_rate", "data_type": "BOOLEAN"},
    {"name": "guardrail_triggered", "data_type": "BOOLEAN"},
    {"name": "classification_confidence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_confidence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "confidence_calibration_error", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "expected_field_presence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "completeness", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {
        "name": "completeness_label",
        "data_type": "CATEGORICAL",
        "categories": [
            {"label": "complete", "value": 1.0},
            {"label": "partial", "value": 0.5},
            {"label": "incomplete", "value": 0.0},
        ],
    },
    {"name": "judge_notes", "data_type": "TEXT"},
    {"name": "classification_quality", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {
        "name": "classification_correct",
        "data_type": "CATEGORICAL",
        "categories": [
            {"label": "correct", "value": 1.0},
            {"label": "ambiguous", "value": 0.5},
            {"label": "incorrect", "value": 0.0},
        ],
    },
    {"name": "extraction_correctness", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {
        "name": "extraction_correctness_label",
        "data_type": "CATEGORICAL",
        "categories": [
            {"label": "accurate", "value": 1.0},
            {"label": "partial", "value": 0.5},
            {"label": "inaccurate", "value": 0.0},
        ],
    },
    # Core run metrics — computed for EVERY run (success, review, failed,
    # aborted) and persisted to the catalog regardless of tracing backend, so
    # runs can be compared against one another offline.
    {"name": "run_aborted", "data_type": "BOOLEAN"},
    {"name": "run_duration_seconds", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "total_tokens", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "estimated_cost_usd", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "llm_call_count", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "classification_attempts", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "extraction_attempts", "data_type": "NUMERIC", "min_value": 0.0},
    # Deterministic field-type-aware extraction scoring (issues #4/#5) —
    # emitted for grounded runs alongside the LLM-as-a-Judge evaluators.
    {"name": "extraction_field_score", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_overall_score", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_needs_judge_review", "data_type": "BOOLEAN"},
    {"name": "entity_list_precision", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "entity_list_recall", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    # Factuality audit (every reported value must match a GT label or be
    # grounded in the source document) + category presence.
    {"name": "extraction_overall_verified_precision", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_hallucination_rate", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_category_presence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    # Dedicated specialist-suite extras (llm-dojo-scoring 0.9.0): Enron
    # topic/sentiment on correspondence, MAUD per-question extraction on
    # merger_agreement. Not extraction fields — peeled before typed scoring.
    {"name": "content_topic_accuracy", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "content_topic_f1_macro", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "sentiment_accuracy", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "sentiment_f1_macro", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "maud_question_accuracy", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "maud_question_macro_accuracy", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "maud_clause_presence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "maud_valid_class_rate", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "maud_category_accuracy", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    # Intake clerk (llm-dojo-scoring PR #5): get_suite("intake") scores the
    # pre-sorter normalize against clerk gold. Live method is deterministic;
    # completeness/changed/messy/count flags still land on the trace.
    {"name": "intake_prep_completeness", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "intake_changed_rate", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "intake_messy_rate", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "intake_hyphen_unwraps", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "intake_collapsed_blanks", "data_type": "NUMERIC", "min_value": 0.0},
    # Dojo 0.10.0+: field-micro P/R/F1/F2 + insurance claims extras.
    # v0.14.0 adds citation/inclusion/ground_truth on these names; it does
    # not add `field_presence` (honesty gap — score_extraction does not emit it).
    {"name": "extraction_precision", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_recall", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_f1", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_f2", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    # The scoring node family's tier-0 schema adherence (unified audit row, roadmap M12).
    {"name": "schema_adherence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "entity_list_f1", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "determination_consistency", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "amount_exactness", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    # --- LegalBench evaluation suite (legalbench/) ----------------------
    # Run-level scores attached to the per-run Langfuse trace by the suite's
    # runner; deterministic, computed locally (never LLM-graded).
    {"name": "legalbench_accuracy", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "legalbench_macro_f1", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "legalbench_calibration_error", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "legalbench_n_questions", "data_type": "NUMERIC", "min_value": 0.0},
    {"name": "legalbench_task", "data_type": "TEXT"},
]


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


SCORE_CONFIGS = _freeze(SCORE_CONFIGS)
# Existing Mailroom verdict label, computed by Dojo's pipeline_verdict helper.
# It is an emitted transport score but not a Dojo metric-registry entry.
DERIVED_SCORE_CONFIGS = _freeze([{
    "name": "deterministic_verdict", "data_type": "CATEGORICAL",
    "categories": [{"label": label, "value": value} for label, value in
                   (("CORRECT", 1.0), ("PARTIAL", 0.5), ("MISS", 0.0))],
}])
ALL_SCORE_CONFIGS = SCORE_CONFIGS + DERIVED_SCORE_CONFIGS
SCORE_BY_NAME = MappingProxyType({spec["name"]: spec for spec in ALL_SCORE_CONFIGS})

def validate_score(name: str, value, data_type: str | None = None):
    """Reject unknown names, type drift, non-finite and out-of-range values."""
    spec = SCORE_BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown Mailroom score: {name!r}")
    kind = spec["data_type"]
    if data_type is not None and kind != data_type:
        raise ValueError(f"score {name!r} type must be {kind}, got {data_type}")
    if kind == "TEXT":
        if not isinstance(value, str):
            raise ValueError(f"score {name!r} requires text")
    elif kind == "CATEGORICAL":
        if not isinstance(value, str) or not any(value == row["label"] for row in spec["categories"]):
            raise ValueError(f"score {name!r} has unknown category: {value!r}")
    elif kind == "BOOLEAN":
        if value not in (0, 1, False, True):
            raise ValueError(f"score {name!r} requires boolean")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"score {name!r} requires finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"score {name!r} requires finite number")
        if value < spec.get("min_value", float("-inf")) or value > spec.get("max_value", float("inf")):
            raise ValueError(f"score {name!r} outside configured range")
    return spec
