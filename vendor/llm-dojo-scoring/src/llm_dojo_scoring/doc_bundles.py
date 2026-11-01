"""Document-type-aware metric bundles (KANBAN-067).

Where :data:`~llm_dojo_scoring.bundles.BUILTIN_BUNDLES` groups metrics by
TASK (classify / extract / audit / ...), ``DOC_TYPE_BUNDLES`` groups them by
the KIND OF DOCUMENT flowing through the mailroom. Same :class:`Bundle`
shape, same registry validation at load — a separate namespace so task
bundles and doc bundles can evolve independently.

Honesty mandate (KANBAN-067): a doc type gets type-specific metrics ONLY
where real, checkable scoring logic exists today. Types whose specialist
scorers are still future work say so in their description instead of
pretending. Enron topic/sentiment, MAUD per-question extraction, and
WER/CER now ship as real scorers. New scorers land in the matching key —
the registry is the modular extension point.

Doc-type → dataset grounding (published merge:
Lucius-Morningstar/mailroom-dataset, 3,302 GT rows):

==================  =====================================================
doc type            corpus / benchmark grounding
==================  =====================================================
contract            CUAD v1 (509 rows, 25 families, 41 clause categories)
                    — contracteval + laziness metrics; MAUD extras when
                    maud_clause_labels / maud_clauses are present
merger_agreement    MAUD (152 rows, consideration subclass + 22 question
                    keys) — per-question exact / valid-class / presence
correspondence      Enron (110 rows, form + topic + sentiment) —
                    content_topic + sentiment_label accuracy / macro-F1
due_diligence       zero rows in the published merge — extraction schema
corporate_record    450 rows (10 record-shape subclasses; per-subclass entity
                    schemas, key-entity GT unlabeled) — no external
                    extraction benchmark
compliance_filing   zero rows in the published merge — extraction schema
court_opinion       zero rows in the published merge; LegalBench metrics
insurance_claim     CMS DE-SynPUF (400 rows, source-table subclass) —
                    determination_consistency + amount_exactness computed;
                    GT is homogeneous (all-approved / empty denials)
==================  =====================================================
"""

from __future__ import annotations

from .bundles import Bundle
from .registry import Registry, load_registry

__all__ = [
    "DOC_TYPE_BUNDLES",
    "DOC_TYPES",
    "get_doc_bundle",
    "list_doc_types",
    "validate_doc_bundle",
]

#: The canonical document classes, in mailroom taxonomy order (the sorter's
#: 7 labels) plus ``merger_agreement`` — the MAUD-grounded class scored
#: as ``MergerAgreementExtraction`` (mailroom-issues #237 / #238).
DOC_TYPES: tuple[str, ...] = (
    "contract",
    "corporate_record",
    "due_diligence",
    "correspondence",
    "compliance_filing",
    "court_opinion",
    "insurance_claim",
    "merger_agreement",
)


def _doc(
    name: str,
    description: str,
    metric_names: tuple[str, ...],
    agent_overrides: dict[str, tuple[str, ...]] | None = None,
) -> Bundle:
    """Construct a document-prefixed bundle with optional per-agent metric extras."""
    return Bundle(
        name=f"doc:{name}",
        description=description,
        metric_names=metric_names,
        agent_overrides=agent_overrides or {},
    )


_EXTRACTION_BASE: tuple[str, ...] = (
    "extraction_overall_score",
    "extraction_f1",
    "extraction_f2",
    "extraction_precision",
    "extraction_recall",
    "field_presence",
    "entity_list_precision",
    "entity_list_recall",
    "entity_list_f1",
    "verified_precision",
    "completeness",
    "schema_valid",
    "parse_error",
    "success_rate",
    "estimated_cost_usd",
    "cost_per_document",
)


#: Doc-type bundles. ``name`` is conventionally prefixed ``doc:`` so a bundle
#: instance is never confused with a task bundle in logs/dashboards; lookup
#: keys in DOC_TYPE_BUNDLES are the bare doc types.
DOC_TYPE_BUNDLES: dict[str, Bundle] = {
    name: _doc(name, description, metric_names, overrides)
    for name, description, metric_names, overrides in (
        (
            "contract",
            "Contracts — CUAD-grounded; laziness/hallucination surface via "
            "contracts_specialist overrides",
            _EXTRACTION_BASE,
            {
                "contracts_specialist": (
                    "jaccard_similarity",
                    "laziness_rate",
                    "hallucination_rate",
                    "extraction_category_presence",
                    "date_mae_days",
                    "money_mae_usd",
                    "maud_question_accuracy",
                    "maud_question_macro_accuracy",
                    "maud_clause_presence",
                    "maud_valid_class_rate",
                ),
            },
        ),
        (
            "merger_agreement",
            "Merger agreements — MAUD-grounded (EDA: "
            "Exios66/atticus-investigation). Per-question extraction over "
            "the 22 Hub maud_clause_labels keys (exact / valid-class / "
            "presence) plus MergerAgreementExtraction (effective_time, "
            "intent, subject_matter, keywords; no cuad_family / "
            "cuad_clauses).",
            _EXTRACTION_BASE,
            {
                "merger_agreement_specialist": (
                    "date_mae_days",
                    "per_field_scores",
                    "hallucination_rate",
                    "maud_question_accuracy",
                    "maud_question_macro_accuracy",
                    "maud_clause_presence",
                    "maud_valid_class_rate",
                    "maud_category_accuracy",
                ),
            },
        ),
        (
            "correspondence",
            "Correspondence — Enron-grounded (EDA: "
            "Exios66/Enron-Evaluation-Environment): client emails, attorney "
            "demand letters, inter-agency messaging. Content scorers cover "
            "the 11 content_topic labels and 3 sentiment_label classes; "
            "typed extraction (sender/recipient/…) stays a separate schema.",
            _EXTRACTION_BASE,
            {
                "correspondence_specialist": (
                    "date_mae_days",
                    "money_mae_usd",
                    "per_field_scores",
                    "hallucination_rate",
                    "content_topic_accuracy",
                    "content_topic_f1_macro",
                    "sentiment_accuracy",
                    "sentiment_f1_macro",
                ),
            },
        ),
        (
            "due_diligence",
            "Due-diligence materials — no external benchmark (synthetic "
            "samples only); typed-extraction base",
            _EXTRACTION_BASE,
            {
                "due_diligence_specialist": (
                    "date_mae_days",
                    "per_field_scores",
                    "hallucination_rate",
                ),
            },
        ),
        (
            "corporate_record",
            "Corporate records — no *external* extraction benchmark "
            "(450-row GT in 10 record-shape subclasses; per-subclass entity "
            "schemas; unlabeled key-entity fields are excluded from "
            "scoring; do not claim CUAD/MAUD-grade coverage). record_type "
            "is a typed name field plus the subclass catalog.",
            _EXTRACTION_BASE,
            {
                "corporate_records_specialist": (
                    "date_mae_days",
                    "per_field_scores",
                    "hallucination_rate",
                ),
            },
        ),
        (
            "compliance_filing",
            "Compliance filings — no external benchmark (synthetic samples "
            "only); typed-extraction base. Future: deadline/date-field "
            "emphasis via field_presence weighting.",
            _EXTRACTION_BASE,
            {
                "compliance_specialist": (
                    "date_mae_days",
                    "per_field_scores",
                    "hallucination_rate",
                ),
            },
        ),
        (
            "court_opinion",
            "Court opinions — LegalBench-grounded; the one doc type with "
            "real benchmark metrics shipping today",
            _EXTRACTION_BASE,
            {
                "court_opinions_specialist": (
                    "legalbench_accuracy",
                    "legalbench_macro_f1",
                    "date_mae_days",
                    "hallucination_rate",
                ),
            },
        ),
        (
            "insurance_claim",
            "Insurance claims — CMS DE-SynPUF grounded. Field-micro P/R/F1/F2 "
            "plus determination_consistency and amount_exactness. HONEST GAP: "
            "published GT is homogeneous (all coverage_determination=approved, "
            "empty denial_reasons), so consistency is degenerate on "
            "GT-shaped predictions rather than a missing scorer.",
            _EXTRACTION_BASE,
            {
                "insurance_claims_specialist": (
                    "date_mae_days",
                    "money_mae_usd",
                    "per_field_scores",
                    "hallucination_rate",
                    "determination_consistency",
                    "amount_exactness",
                ),
            },
        ),
    )
}


def get_doc_bundle(
    doc_type: str,
    *,
    registry: Registry | None = None,
    validate: bool = True,
) -> Bundle:
    """Return the doc-type bundle; ``KeyError`` on unknown doc types."""
    try:
        bundle = DOC_TYPE_BUNDLES[doc_type]
    except KeyError:
        raise KeyError(
            f"unknown doc type {doc_type!r}; known: {sorted(DOC_TYPE_BUNDLES)}"
        ) from None
    if validate:
        validate_doc_bundle(bundle, registry=registry)
    return bundle


def list_doc_types() -> list[str]:
    """Return supported document bundle types in sorted order."""
    return sorted(DOC_TYPE_BUNDLES)


def validate_doc_bundle(
    bundle: Bundle,
    *,
    registry: Registry | None = None,
) -> list[str]:
    """Every metric (incl. per-agent extras) must resolve in the registry."""
    reg = registry or load_registry()
    for name in bundle.metric_names:
        reg.get(name)
    for extras in bundle.agent_overrides.values():
        for extra in extras:
            reg.get(extra)
    return list(bundle.metric_names)
