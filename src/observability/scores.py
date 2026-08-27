"""Quality scores for document runs.

Backend-agnostic helpers to attach evaluation scores to Langfuse traces
(no-ops when observability is disabled). Score configs are created
idempotently via the SDK so the project always has a canonical scoring schema.

Two score origins:

- **Production** (`emit_pipeline_scores`): self-evident signals computed inside
  a run with no ground truth needed — parse errors, schema validity, routing
  outcome, and the confidence values (so calibration dashboards work offline).
- **Pilot** (`scripts/run_pilot.py`, `scripts/run_completeness_judge.py`):
  ground-truth-derived scores (class/stage correctness, calibration error,
  completeness) attached to the deterministic trace id.

All helpers silently no-op when Langfuse is not the active backend, matching
the tracing facade in `observability/tracing.py`.
"""

import structlog

logger = structlog.get_logger(__name__)

_configs_ensured: set[str] = set()
_last_warmup_attempt: float = 0.0
_WARMUP_RETRY_SECONDS = 600.0  # sticky-bounded retry (O-1): at most once per 10 min

# Canonical scoring schema, mirrored as Langfuse score configs by
# `ensure_score_configs()`. Keys: name, data_type, optional min/max/categories.
SCORE_CONFIGS: list[dict] = [
    {"name": "class_correct", "data_type": "BOOLEAN"},
    {"name": "stage_correct", "data_type": "BOOLEAN"},
    {"name": "parse_error", "data_type": "BOOLEAN"},
    {"name": "schema_valid", "data_type": "BOOLEAN"},
    {"name": "stage_completed", "data_type": "BOOLEAN"},
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
    # Dojo 0.10.0: field-micro P/R/F1/F2 + insurance claims extras.
    {"name": "extraction_precision", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_recall", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_f1", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_f2", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
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

# Langfuse Cloud rejects score *config* names over 35 characters
# (`extraction_overall_verified_precision` is 40). Keep the canonical
# Python/dojo name in SCORE_CONFIGS; emit this alias on the wire.
LANGFUSE_SCORE_NAME_ALIASES = {
    "extraction_overall_verified_precision": "extraction_verified_precision",
}


def langfuse_score_name(name: str) -> str:
    """Name actually sent to Langfuse (may be a short transport alias)."""
    return LANGFUSE_SCORE_NAME_ALIASES.get(name, name)

# KANBAN-061: SCORE_CONFIGS is validated against llm-dojo-scoring's metric
# registry (single source of truth). A name here but not in the registry
# means the schema has drifted — fail loudly at import rather than silently
# emitting unregistered metrics.
try:
    from llm_dojo_scoring import load_registry as _load_registry

    _unregistered = [
        c["name"] for c in SCORE_CONFIGS if c["name"] not in _load_registry().metrics
    ]
    if _unregistered:
        raise RuntimeError(
            "SCORE_CONFIGS contains names missing from the llm-dojo-scoring "
            f"registry: {_unregistered}. Register them upstream or remove "
            "them here."
        )
    logger.debug(
        "score_configs_validated",
        count=len(SCORE_CONFIGS),
        registry="llm-dojo-scoring",
    )
except ImportError:
    # Package not importable in this environment (e.g. docs builds);
    # skip validation rather than block module import.
    pass


def is_enabled() -> bool:
    from observability.tracing import resolve_provider_name

    return resolve_provider_name() == "langfuse"


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    if not is_enabled():
        return None
    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        return None
    return client


def ensure_score_configs() -> list[str]:
    """Create any missing score configs. Idempotent and process-cached — safe
    to call on every startup. Returns the names of configs that exist after
    the call."""
    if _configs_ensured:
        return list(_configs_ensured)
    client = _client()
    if client is None:
        return []
    try:
        # Fetch current configs once so duplicate creation is avoided.
        try:
            page = client.api.score_configs.get(limit=100)
            existing = [c.name for c in (page.data or [])]
        except Exception:
            existing = []
        created = list(existing)
        for spec in SCORE_CONFIGS:
            wire_name = langfuse_score_name(spec["name"])
            if spec["name"] in existing or wire_name in existing:
                continue
            kwargs = {"name": wire_name, "data_type": spec["data_type"]}
            if spec.get("min_value") is not None:
                kwargs["min_value"] = spec["min_value"]
            if spec.get("max_value") is not None:
                kwargs["max_value"] = spec["max_value"]
            if spec.get("categories"):
                from langfuse.api.commons.types.config_category import ConfigCategory

                kwargs["categories"] = [
                    ConfigCategory(value=c["value"], label=c["label"]) for c in spec["categories"]
                ]
            client.api.score_configs.create(**kwargs)
            created.append(wire_name)
            logger.info("score_config_created", name=wire_name, data_type=spec["data_type"])
    except Exception:
        logger.warning("score_config_creation_failed", exc_info=True)
    _configs_ensured.update(created)
    return created


def warmup_score_configs(blocking: bool = False) -> None:
    """Warm the score-config schema OFF the document path (O-1).

    ``ensure_score_configs()`` previously ran inside the first per-document
    run — a synchronous 29-call Langfuse storm that stalled every document
    when Langfuse was down-but-hanging, and re-stormed on every document when
    connection was refused (the cache stayed empty). This variant:
      - never blocks the document path (background thread unless blocking),
      - is sticky-bounded: at most one attempt per 10 minutes regardless of
        outcome, so a dead backend cannot churn on every document,
      - failure ⇒ skip: the document proceeds untraced rather than stalling.
    """
    import threading
    import time

    global _last_warmup_attempt
    if _configs_ensured:
        return
    now = time.monotonic()
    if now - _last_warmup_attempt < _WARMUP_RETRY_SECONDS:
        return  # sticky backoff — last attempt too recent (success or failure)
    _last_warmup_attempt = now

    def _run():
        try:
            ensure_score_configs()
        except Exception:
            logger.warning("score_config_warmup_failed", exc_info=True)

    if blocking:
        _run()
    else:
        threading.Thread(target=_run, name="score-config-warmup", daemon=True).start()


def score_trace(
    name: str,
    value,
    *,
    data_type: str | None = None,
    comment: str | None = None,
    config_id: str | None = None,
    score_id: str | None = None,
) -> None:
    """Attach a score to the currently active trace (inside a pipeline_trace
    block). No-ops when tracing is disabled."""
    client = _client()
    if client is None:
        return
    try:
        client.score_current_trace(
            name=name,
            value=value,
            data_type=data_type,
            comment=comment,
            config_id=config_id,
            score_id=score_id,
        )
        logger.debug("score_attached", name=name, value=value)
    except Exception:
        logger.warning("score_attach_failed", name=name, exc_info=True)


def create_trace_score(
    trace_id: str,
    name: str,
    value,
    *,
    data_type: str | None = None,
    comment: str | None = None,
    config_id: str | None = None,
    score_id: str | None = None,
    observation_id: str | None = None,
) -> None:
    """Attach a score to a trace by id (offline/pilot scoring — no active
    tracing context required). No-ops when tracing is disabled."""
    client = _client()
    if client is None:
        return
    try:
        client.create_score(
            trace_id=trace_id,
            name=langfuse_score_name(name),
            value=value,
            data_type=data_type,
            comment=comment,
            config_id=config_id,
            score_id=score_id,
            observation_id=observation_id,
        )
        logger.debug("score_created_for_trace", trace_id=trace_id, name=name, value=value)
    except Exception:
        logger.warning("score_creation_failed", trace_id=trace_id, name=name, exc_info=True)


def validate_extraction(doc_type: str, extracted_data: dict | None) -> dict:
    """Check an extraction against the doc type's pydantic schema.

    Returns {"parse_error": bool, "schema_valid": bool}. `parse_error` is set
    when the specialist itself flagged a JSON parse failure; `schema_valid` is
    false when the data fails to validate against the schema.
    """
    parsed = {
        "parse_error": bool(extracted_data and extracted_data.get("_parse_error")),
        "schema_valid": False,
    }
    if parsed["parse_error"]:
        # A failed JSON parse means no trustworthy extraction to validate.
        return parsed
    if not doc_type or not extracted_data:
        return parsed
    from schemas.documents import get_extraction_schema

    model = get_extraction_schema(doc_type)
    if model is None:
        # No schema registered: this is not a live extractable class
        # (unknown / retired / hallucinated). Treating it as valid used to
        # let junk payloads look schema-clean and retry instead of parking.
        parsed["schema_valid"] = False
        return parsed
    try:
        model.model_validate(extracted_data)
        parsed["schema_valid"] = True
    except Exception:
        parsed["schema_valid"] = False
    return parsed


def compute_run_metrics(state: dict, started_at: float, ended_at: float) -> dict:
    """Core per-run metrics, computed for EVERY finished run regardless of the
    tracing backend. Consumed by `emit_pipeline_scores` (Langfuse attachment
    stays backend-gated) and persisted to the catalog."""
    from pipeline.limits import estimate_cost, usage_summary

    usage = usage_summary()
    return {
        "run_aborted": int(bool(state.get("run_aborted"))),
        "run_duration_seconds": round(max(0.0, ended_at - started_at), 1),
        "total_tokens": int(usage["total"]),
        "llm_call_count": int(usage["calls"]),
        "estimated_cost_usd": float(estimate_cost()),
        "classification_attempts": int(state.get("classification_attempts", 0)),
        "extraction_attempts": int(state.get("extraction_attempts", 0)),
    }


def _score_data_type(name: str) -> str | None:
    for spec in SCORE_CONFIGS:
        if spec["name"] == name:
            return spec["data_type"]
    return None


def emit_pipeline_scores(state: dict, metrics: dict | None = None) -> dict:
    """Attach self-evident production scores for a finished run (no ground
    truth required) plus the always-on core run metrics. Called from
    `graph/build_graph.py:run_pipeline`. Returns the computed scores so
    callers can persist them locally too.

    Score computation is backend-agnostic and always runs; only the Langfuse
    trace attachment is gated on the active backend (a no-op otherwise).
    """
    stage = state.get("stage")
    extracted = state.get("extracted_data") or {}
    checks = validate_extraction(state.get("doc_type"), extracted)
    guardrail_fired = bool(state.get("extraction_guardrail")) or bool(state.get("classification_guardrail"))
    scores = {
        "parse_error": int(checks["parse_error"]),
        "schema_valid": int(checks["schema_valid"]),
        "stage_completed": int(stage == "archived"),
        "guardrail_triggered": int(guardrail_fired),
    }
    cls_conf = state.get("classification_confidence")
    if isinstance(cls_conf, (int, float)) and not isinstance(cls_conf, bool):
        scores["classification_confidence"] = float(cls_conf)
    ext_conf = state.get("extraction_confidence")
    if isinstance(ext_conf, (int, float)) and not isinstance(ext_conf, bool):
        scores["extraction_confidence"] = float(ext_conf)
    if metrics:
        for name, value in metrics.items():
            if value is None:
                continue
            if isinstance(value, bool):
                scores[name] = int(value)
            elif isinstance(value, (int, float)):
                scores[name] = value
    if is_enabled():
        for name, value in scores.items():
            score_trace(name, value, data_type=_score_data_type(name))
    return scores
