"""llm-dojo-scoring — dedicated scoring, error analysis, visualization, and
interpretation suite for LLM document pipelines.

Import into llm-mailroom (and the vendored sandbox) to replace the project-local
scoring code (``src/field_scoring.py``, ``src/metrics.py``, ``src/bootstrap.py``,
``src/cost_models.py``, ``src/scorers.py``, ``src/experiment_log.py``,
``src/taxonomy.py``, ``agents/sorter_agent.py`` equivalence constants,
``scripts/reporting/export_experiment_results.py``) with one shared library.
"""

from __future__ import annotations

# Vendor and source checkouts can coexist with unrelated installed metadata.
# Report the version of the code actually loaded; release tests pin pyproject parity.
__version__ = "0.27.2"
from ._layout import install_legacy_aliases

install_legacy_aliases()

from . import config, prompts  # noqa: E402
from .schemas import corpus, equivalences  # noqa: E402
from .scoring import asr, claims_consistency, classification, content_scoring, extraction_metrics, field_scoring, suites, tasks, trace_knobs  # noqa: E402
from .pipeline import archive, intake, mailroom  # noqa: E402
from .metrics import bundles, doc_bundles, emitter, profiles, pruning, registry, score_changelog  # noqa: E402
from .analysis import bootstrap, cost, diagnostics, error_analysis, experiment, export, failure_modes, grid, interpret, io, report, serving, visualize  # noqa: E402
from .integrations import langfuse_sync, phoenix_sync  # noqa: E402

# Convenience re-exports (the most common entry points).
from .analysis.bootstrap import bootstrap_ci, delta_significance, wilson_ci
from .scoring.classification import (
    accuracy,
    binary_metrics,
    confusion_matrix,
    exact_match,
    fbeta,
    macro_accuracy,
    macro_prf,
    normalize_label,
    per_class_stats,
)
from .analysis.cost import estimate_cost, tokens_summary
from .schemas.equivalences import (
    equivalent_doc_subclasses,
    equivalent_subtypes,
    normalize_doc_subclass,
    normalize_subtype,
)
from .analysis.failure_modes import (
    classify_docclass_failure,
    classify_failure,
    per_subtype_accuracy,
    summarize_failures,
)
from .schemas.intents import (
    INTENT_ALIASES,
    INTENT_DESCRIPTIONS,
    INTENT_LABELS,
    normalize_intent,
)
from .scoring.field_scoring import (
    EntityListScore,
    embed_texts,
    ExtractionScoreResult,
    FIELD_SCORERS,
    field_is_ambiguous,
    get_ambiguous_band,
    get_field_types,
    get_type_bands,
    is_entity_list,
    normalize_text,
    parse_date,
    parse_money,
    score_category_presence,
    score_extraction,
    score_field,
    score_entity_list,
    warm_embedding_model,
)
from .pipeline.archive import (
    ARCHIVE_HASH_VERSION,
    ARCHIVE_SCORING_METHOD,
    archivist_sign_off,
    archive_entry_hash,
    format_audit_entry,
    prepare_archivist_handoff,
    score_archive_block,
    upsert_archive_scoring,
)
from .pipeline.sandbox_schema_adherence import assess_extraction_payload
from .scoring.doc_scorers import get_doc_scorer
from .scoring.doc_scorers.dispatch import score_document
from .scoring.extraction_metrics import (
    extraction_binary_metrics,
    mean_entity_list_f1,
    merge_extraction_counts,
)
from .scoring.claims_consistency import (
    amount_exactness,
    determination_consistency,
    score_claims_extras,
)
from .config import (
    Settings,
    TraceKnobSettings,
    clear_settings_cache,
    configure,
    configure_from_taxonomy,
    get_settings,
    load_settings,
)
from .scoring.tasks import (
    chained_composite,
    chained_summary,
    court_opinion_score,
    get_jaccard,
    legalbench_score,
    maud_docclass_score,
    maud_extraction_score,
    maud_question_score,
    multiclass_score,
    normalize_task_answer,
    score_task,
    task_kind,
)
from .scoring.reviewer import (
    CONTINUE_VERDICTS,
    DECISION_VERDICTS,
    HUMAN_VERDICTS,
    ReviewRecord,
    review_record_from_run_item,
    score_review_lane,
)
from .scoring.rescore import (
    RescoreError,
    diff_scorecards,
    rescore_file,
    rescore_record,
    rescore_records,
)
from .analysis.calibration import (
    auroc,
    brier_score,
    clopper_pearson_upper,
    cross_fit_cutoffs,
    cutoff_at_target,
    cutoff_for_precision,
    expected_calibration_error,
    reliability_bins,
    required_samples,
    spearman,
)
from .scoring.asr import (
    character_error_rate,
    score_transcription,
    word_error_rate,
)
from .scoring.content_scoring import (
    is_valid_maud_answer,
    peel_non_extraction_fields,
    score_content_topic,
    score_correspondence_content,
    score_maud_extraction,
    score_sentiment,
)
from .schemas.maud import (
    MAUD_ANSWER_CLASSES,
    MAUD_CATALOG_GENERATED,
    MAUD_CATALOG_ROWS,
    MAUD_CATALOG_SOURCE,
    MAUD_QUESTION_KEYS_BY_DOC_TYPE,
    MAUD_VARIABLE_CLASS_QUESTIONS,
    canonical_maud_class,
    is_maud_class,
    maud_class_index,
    maud_question_catalog,
)
from .schemas.gt_metadata import (
    ABSENT_PRESENCE_STATUSES,
    ANNOTATION_KEYS,
    CUAD_PRESENCE_KEY,
    GT_PRESENCE_KEY,
    derive_presence_from_gt,
    gt_presence_map,
    is_empty_value,
    normalize_field_values,
    parse_gt_fields,
    parse_json_container,
    presence_expectations_from_cuad_labels,
    scoring_gt_fields,
)
from .scoring.trace_knobs import (
    capture_trace_knobs,
    confidence_calibration_error,
    parse_confidence,
    parse_reasoning,
)
from .pipeline.intake import (
    INTAKE_SPAN_KEYS,
    apply_intake,
    deterministic_normalize,
    looks_messy,
    score_intake,
)
from .metrics.bundles import BUILTIN_BUNDLES, Bundle, bundle_metric_names, get_bundle, validate_bundle
from .metrics.doc_bundles import (
    DOC_TYPES,
    DOC_TYPE_BUNDLES,
    get_doc_bundle,
    list_doc_types,
    validate_doc_bundle,
)
from .metrics.emitter import (
    Emitter,
    get_emitter,
    LangfuseSink,
    LocalManifestSink,
    reset_default_emitter,
    ScoreRecord,
)
from .metrics.profiles import AgentProfile, get_profile, list_profiles, load_profiles
from .metrics.pruning import (
    DEFAULT_DASHBOARD_TIER,
    dashboard_metrics,
    headline_metrics,
    prune_metrics,
    prune_records,
)
from .metrics.score_changelog import (
    SCORE_CHANGELOG,
    score_changes_between,
    score_changes_for_metric,
)
from .schemas.corpus import (
    CORPUS_DOC_TYPES,
    DOC_TYPE_SUBCLASSES,
    normalize_corpus_subclass,
    suite_schema,
)
from .schemas.sorter_guide import (
    CORRESPONDENCE_SUBCLASS_PRECEDENCE,
    SORTER_SUBCLASS_GUIDE,
    UNANNOTATED_SUBCLASSES,
    format_sorter_subclass_guide,
    sorter_subclass_keys,
    subclass_scorable,
)
from .schemas.cuad_schema import (
    CUAD_CATEGORIES,
    CUAD_FAMILIES,
    cuad_category,
    cuad_family,
    cuad_json_schema,
    cuad_questions,
    render_cuad_question_block,
)
from .schemas.maud_schema import (
    MAUD_CONSIDERATION_SUBCLASSES,
    MAUD_QUESTIONS,
    maud_answers_to_clauses,
    maud_json_schema,
    maud_question,
    maud_questions,
    parse_maud_clause,
    render_maud_question_block,
)
from .schemas.corporate_record_schema import (
    SUBCLASS_SCHEMAS as CORPORATE_RECORD_SUBCLASS_SCHEMAS,
    audit_gt_rows as audit_corporate_record_labels,
    corporate_record_json_schema,
    render_subclass_block as render_corporate_record_block,
    scored_field_types as corporate_record_field_types,
    subclass_schema as corporate_record_subclass_schema,
)
from .schemas.schema_contract import contract_empty_payload, contract_violations
from .schemas.scored_enums import (  # noqa: E402
    SCORED_ENUM_FIELDS, enum_hint_violations, scored_enum_hint, scored_enum_schema,
    scored_enum_schemas, scored_vocabulary,
)
from .schemas.extraction_schemas import (  # noqa: E402
    ExtractionSchema, FieldSchema, extraction_schema,
    extraction_subtypes, iter_extraction_schemas, schema_coverage,
)
from .pipeline.mailroom import (
    EXTRACT_CLASS_ALIASES,
    LIVE_DOC_TYPES,
    LIVE_SPECIALISTS,
    NODE_OBSERVATION_TYPES,
    PIPELINE_TRACE,
    align_doc_type,
    langfuse_score_name,
    observation_type_for,
    resolve_extract_class,
    score_aligned_classification,
)
from .scoring.suites import (
    DEFAULT_FIELD_TYPES,
    DEFAULT_SUITES,
    DOC_TYPE_ALIASES,
    LEGACY_FULL_EXTRACTION_FIELD_TYPES,
    SPECIALIST_DOC_TYPES,
    ScoringSuite,
    get_suite,
    list_suites,
    score_suite,
    suite_for_doc_type,
)
from .metrics.registry import (
    ALLOWED_GROUND_TRUTH,
    clear_registry_cache,
    get_registry,
    load_registry,
    MetricDef,
    MetricTier,
    Registry,
)
from .prompts import PromptRecord, get_prompt, list_prompts
from .analysis.serving import (
    CANONICAL_SERVING_KEYS,
    ServingIdentity,
    ServingObservation,
    ServingRun,
    classify_serving_kind,
    compare_serving,
    emit_serving_scorecard,
    pair_comparable_runs,
    score_serving_run,
    serving_card_markdown,
    serving_cost_card,
    serving_scorecard,
    serving_table_markdown,
    serving_table_rows,
    split_local_api,
)
from .analysis.grid import (
    GridDocument,
    GridExperiment,
    build_grid_report,
    grid_scorecard,
    serving_efficiency_rows,
    session_cost_rows,
    specialist_grid_rows,
)

__all__ = [
    "__version__",
    "archive",
    "bootstrap", "classification", "claims_consistency", "config", "content_scoring", "cost", "diagnostics",
    "equivalences", "error_analysis", "experiment", "export", "extraction_metrics", "failure_modes",
    "field_scoring", "io", "interpret", "langfuse_sync", "phoenix_sync",
    "report", "asr", "corpus", "intake", "mailroom", "prompts", "serving",
    "suites", "tasks", "trace_knobs", "visualize", "grid",
    "ARCHIVE_HASH_VERSION", "ARCHIVE_SCORING_METHOD",
    "archive_entry_hash", "archivist_sign_off", "format_audit_entry",
    "prepare_archivist_handoff", "score_archive_block", "upsert_archive_scoring",
    "assess_extraction_payload", "get_doc_scorer", "score_document",
    "bootstrap_ci", "delta_significance", "wilson_ci",
    "accuracy", "binary_metrics", "confusion_matrix", "exact_match",
    "fbeta", "macro_accuracy", "macro_prf", "normalize_label", "per_class_stats",
    "estimate_cost", "tokens_summary",
    "equivalent_doc_subclasses", "equivalent_subtypes",
    "normalize_doc_subclass", "normalize_subtype",
    "classify_docclass_failure", "classify_failure", "per_subtype_accuracy",
    "summarize_failures",
    "EntityListScore", "ExtractionScoreResult", "score_extraction",
    "score_field", "score_entity_list", "field_is_ambiguous",
    "get_field_types", "get_type_bands", "warm_embedding_model",
    "normalize_text", "parse_date", "parse_money", "score_category_presence",
    "is_entity_list", "get_ambiguous_band", "FIELD_SCORERS", "embed_texts",
    "INTENT_LABELS", "INTENT_ALIASES", "INTENT_DESCRIPTIONS", "normalize_intent",
    "extraction_binary_metrics", "mean_entity_list_f1", "merge_extraction_counts",
    "amount_exactness", "determination_consistency", "score_claims_extras",
    "Settings", "TraceKnobSettings", "clear_settings_cache", "configure", "configure_from_taxonomy",

    "get_settings",
    "load_settings",
    "chained_composite", "chained_summary", "court_opinion_score",
    "get_jaccard", "legalbench_score", "maud_docclass_score", "maud_extraction_score",
    "maud_question_score",
    "multiclass_score", "normalize_task_answer", "score_task", "task_kind",
    "CONTINUE_VERDICTS", "DECISION_VERDICTS", "HUMAN_VERDICTS",
    "ReviewRecord", "review_record_from_run_item", "score_review_lane",
    "RescoreError", "diff_scorecards", "rescore_file", "rescore_record", "rescore_records",
    "auroc", "brier_score", "clopper_pearson_upper", "cross_fit_cutoffs",
    "cutoff_at_target", "cutoff_for_precision", "expected_calibration_error",
    "reliability_bins", "required_samples", "spearman",
    "SCORE_CHANGELOG", "score_changes_between", "score_changes_for_metric",
    "BUILTIN_BUNDLES", "Bundle", "bundle_metric_names", "get_bundle",
    "validate_bundle",
    "Emitter", "get_emitter", "LangfuseSink", "LocalManifestSink",
    "reset_default_emitter", "ScoreRecord",
    "AgentProfile", "get_profile", "list_profiles", "load_profiles",
    "DEFAULT_DASHBOARD_TIER", "dashboard_metrics", "headline_metrics",
    "prune_metrics", "prune_records",
    "clear_registry_cache", "get_registry", "load_registry", "MetricDef",
    "MetricTier", "Registry", "ALLOWED_GROUND_TRUTH",
    "PromptRecord", "get_prompt", "list_prompts",
    "ScoringSuite", "DEFAULT_SUITES", "DEFAULT_FIELD_TYPES",
    "LEGACY_FULL_EXTRACTION_FIELD_TYPES",
    "DOC_TYPE_ALIASES", "SPECIALIST_DOC_TYPES",
    "get_suite", "list_suites", "score_suite", "suite_for_doc_type",
    "CORPUS_DOC_TYPES", "DOC_TYPE_SUBCLASSES",
    "CORRESPONDENCE_SUBCLASS_PRECEDENCE", "SORTER_SUBCLASS_GUIDE", "UNANNOTATED_SUBCLASSES",
    "format_sorter_subclass_guide", "sorter_subclass_keys", "subclass_scorable",
    "normalize_corpus_subclass", "suite_schema",
    "LIVE_DOC_TYPES", "LIVE_SPECIALISTS", "PIPELINE_TRACE",
    "EXTRACT_CLASS_ALIASES", "NODE_OBSERVATION_TYPES",
    "align_doc_type", "langfuse_score_name", "observation_type_for",
    "resolve_extract_class", "score_aligned_classification",
    "word_error_rate", "character_error_rate", "score_transcription",
    "score_content_topic", "score_sentiment",
    "peel_non_extraction_fields",
    "score_correspondence_content", "score_maud_extraction",
    "is_valid_maud_answer",
    "MAUD_ANSWER_CLASSES", "MAUD_QUESTION_KEYS_BY_DOC_TYPE",
    "MAUD_VARIABLE_CLASS_QUESTIONS", "MAUD_CATALOG_SOURCE",
    "MAUD_CATALOG_GENERATED", "MAUD_CATALOG_ROWS",
    "canonical_maud_class", "is_maud_class", "maud_class_index",
    "maud_question_catalog",
    "CUAD_CATEGORIES", "CUAD_FAMILIES", "cuad_category", "cuad_family",
    "cuad_json_schema", "cuad_questions", "render_cuad_question_block",
    "MAUD_CONSIDERATION_SUBCLASSES", "MAUD_QUESTIONS", "maud_answers_to_clauses",
    "maud_json_schema", "maud_question", "maud_questions", "parse_maud_clause",
    "render_maud_question_block",
    "CORPORATE_RECORD_SUBCLASS_SCHEMAS", "audit_corporate_record_labels",
    "corporate_record_json_schema", "render_corporate_record_block",
    "corporate_record_field_types", "corporate_record_subclass_schema",
    "contract_empty_payload", "contract_violations",
    "SCORED_ENUM_FIELDS", "enum_hint_violations", "scored_enum_hint", "scored_enum_schema",
    "scored_enum_schemas", "scored_vocabulary",
    "ExtractionSchema", "FieldSchema", "extraction_schema",
    "extraction_subtypes", "iter_extraction_schemas", "schema_coverage",
    "parse_gt_fields", "scoring_gt_fields", "normalize_field_values",
    "parse_json_container", "is_empty_value", "gt_presence_map",
    "presence_expectations_from_cuad_labels", "derive_presence_from_gt",
    "ANNOTATION_KEYS", "ABSENT_PRESENCE_STATUSES",
    "CUAD_PRESENCE_KEY", "GT_PRESENCE_KEY",
    "capture_trace_knobs", "confidence_calibration_error",
    "parse_confidence", "parse_reasoning",
    "apply_intake", "deterministic_normalize", "looks_messy", "score_intake",
    "INTAKE_SPAN_KEYS",
    "CANONICAL_SERVING_KEYS", "ServingIdentity", "ServingObservation",
    "ServingRun", "classify_serving_kind", "compare_serving",
    "emit_serving_scorecard", "pair_comparable_runs", "score_serving_run",
    "serving_card_markdown", "serving_cost_card", "serving_scorecard",
    "serving_table_markdown", "serving_table_rows", "split_local_api",
    "GridDocument", "GridExperiment", "build_grid_report", "grid_scorecard",
    "serving_efficiency_rows", "session_cost_rows", "specialist_grid_rows",
]

# Additive sandbox benchmark API; existing task/suite entry points keep their rubrics.
from .pipeline.sandbox_scoring import score_cuad as score_sandbox_cuad
from .pipeline.sandbox_scoring import score_maud as score_sandbox_maud

__all__ += ["score_sandbox_cuad", "score_sandbox_maud"]

from .pipeline.sandbox_scoring import sandbox_metric_scores
__all__ += ["sandbox_metric_scores"]

from .pipeline.sandbox_scoring import aggregate_sandbox_scores
__all__ += ["aggregate_sandbox_scores"]
