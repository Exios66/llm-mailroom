"""Wire deterministic field scoring into Langfuse (GitHub issue #5).

The scoring LOGIC is backend-agnostic and lives in
``observability/field_scoring.py`` (issue #4); this module is the wiring
layer: it registers the score configs (delegating to the canonical registry
in ``observability/scores.py``) and attaches the deterministic scores to a
trace via the existing backend-gated ``create_trace_score`` helper, so every
helper no-ops cleanly when Langfuse is not the active backend.

Score configs registered (all auto-created by ``ensure_field_score_configs``):

- ``extraction_field_score`` (NUMERIC 0-1): per-field deterministic score
  (date/money/id exact-after-normalize, name fuzzy match, free-text token F1,
  entity-list bipartite F1).
- ``extraction_overall_score`` (NUMERIC 0-1): mean of per-field scores.
- ``extraction_needs_judge_review`` (BOOLEAN): any field landed in the
  ambiguous band (0.5-0.85 by default) and should be escalated to the
  LLM-as-judge evaluator rather than trusted from the deterministic score.
- ``entity_list_precision`` / ``entity_list_recall`` (NUMERIC 0-1): per
  list-valued field, after optimal bipartite matching.

Call ``score_and_log_extraction`` from the same place the pipeline emits its
other ground-truth scores (the grounded-extraction path in
``graph/build_graph.py:run_pipeline``), so all deterministic scores land on
the same trace as ``expected_field_presence`` and are comparable in the
Langfuse UI.
"""

from __future__ import annotations

import structlog

from observability.field_scoring import ExtractionScoreResult, score_extraction

logger = structlog.get_logger(__name__)

FIELD_SCORE_CONFIGS = [
    {
        "name": "extraction_field_score",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": (
            "Deterministic per-field similarity score (date/money/id exact-after-normalize, "
            "name fuzzy match, free-text token F1, containment, entity-list bipartite)."
        ),
    },
    {
        "name": "extraction_overall_score",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": "Mean of per-field deterministic scores for one extraction.",
    },
    {
        "name": "extraction_needs_judge_review",
        "data_type": "BOOLEAN",
        "description": (
            "True if any field landed in the ambiguous band (0.5-0.85) and should be escalated "
            "to the LLM-as-judge evaluator rather than trusted from the deterministic score alone."
        ),
    },
    {
        "name": "entity_list_precision",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": "Precision of an entity-list field after optimal bipartite matching.",
    },
    {
        "name": "entity_list_recall",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": "Recall of an entity-list field after optimal bipartite matching.",
    },
    {
        "name": "extraction_overall_verified_precision",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": (
            "Mean verified_precision across every audited content field — each reported value "
            "must match a GT label or be grounded in the source document (factuality guard)."
        ),
    },
    {
        "name": "extraction_hallucination_rate",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": "Mean hallucination_rate across every audited content field.",
    },
    {
        "name": "extraction_category_presence",
        "data_type": "NUMERIC",
        "min_value": 0.0,
        "max_value": 1.0,
        "description": "Share of expected-True presence categories whose clause text is matched.",
    },
]


def ensure_field_score_configs() -> list[str]:
    """Idempotent: register the field-scoring configs in the Langfuse project.

    The canonical registry lives in ``observability/scores.py:SCORE_CONFIGS``
    (mirrored here for documentation/verification); this delegates so there is
    exactly one source of truth for score configs.
    """
    from observability.scores import ensure_score_configs

    return ensure_score_configs()


def score_and_log_extraction(
    trace_id: str | None,
    doc_class: str,
    field_types: dict[str, str],
    predicted: dict,
    expected: dict,
    *,
    observation_id: str | None = None,
    matter_id: str | None = None,
    doc_text: str | None = None,
    presence_expectations: dict | None = None,
) -> ExtractionScoreResult:
    """Score one extraction deterministically and push every score to Langfuse,
    attached to the given trace.

    ``doc_text`` enables the factuality audit (every reported value must match
    a ground-truth label or be grounded in the source document) —
    ``extraction_overall_verified_precision`` / ``extraction_hallucination_rate``
    are emitted from it. ``presence_expectations`` (CUAD-style presence
    categories) enables ``extraction_category_presence`` when provided.

    No-ops (without crashing) when Langfuse is not the active backend — the
    result is still returned so callers can gate on ``needs_judge_review``
    regardless of the tracing backend.
    """
    from observability.scores import create_trace_score, is_enabled

    result = score_extraction(
        doc_class, field_types, predicted, expected, doc_text=doc_text
    )
    if not is_enabled():
        return result

    common_kwargs = {"trace_id": trace_id}
    if observation_id:
        common_kwargs["observation_id"] = observation_id

    for field_name, value in result.field_scores.items():
        create_trace_score(
            name="extraction_field_score",
            value=value,
            comment=f"field={field_name} doc_class={doc_class}",
            **common_kwargs,
        )

    if result.overall_score is not None:
        create_trace_score(
            name="extraction_overall_score",
            value=result.overall_score,
            comment=f"doc_class={doc_class} n_fields={len(result.field_scores)}",
            **common_kwargs,
        )

    create_trace_score(
        name="extraction_needs_judge_review",
        value=bool(result.ambiguous_fields),
        comment=f"ambiguous_fields={result.ambiguous_fields}" if result.ambiguous_fields else None,
        **common_kwargs,
    )

    for field_name, el_score in result.entity_list_scores.items():
        create_trace_score(
            name="entity_list_precision",
            value=el_score.precision,
            comment=(
                f"field={field_name} unmatched_pred={el_score.unmatched_predicted} "
                f"unmatched_exp={el_score.unmatched_expected}"
            ),
            **common_kwargs,
        )
        create_trace_score(
            name="entity_list_recall",
            value=el_score.recall,
            comment=f"field={field_name}",
            **common_kwargs,
        )

    # Factuality audit: mean verified_precision / hallucination_rate over every
    # audited content field (list items + scalars the model actually reported).
    if result.overall_verified_precision is not None:
        create_trace_score(
            name="extraction_overall_verified_precision",
            value=result.overall_verified_precision,
            comment=f"doc_class={doc_class} n_audited={sum(1 for a in result.entity_list_audit.values() if a.get('n_predicted'))}",
            **common_kwargs,
        )
    audited = [
        a["hallucination_rate"] for a in result.entity_list_audit.values()
        if a.get("n_predicted")
    ]
    if audited:
        create_trace_score(
            name="extraction_hallucination_rate",
            value=round(sum(audited) / len(audited), 4),
            comment=f"doc_class={doc_class} n_audited={len(audited)}",
            **common_kwargs,
        )

    if presence_expectations:
        from observability.field_scoring import score_category_presence

        cat_score, _ = score_category_presence(predicted, presence_expectations, field_types)
        create_trace_score(
            name="extraction_category_presence",
            value=cat_score,
            comment=f"doc_class={doc_class}",
            **common_kwargs,
        )

    logger.debug(
        "field_scores_attached",
        trace_id=trace_id,
        doc_class=doc_class,
        overall=result.overall_score,
        needs_judge_review=result.needs_judge_review,
        matter_id=matter_id,
    )
    return result
