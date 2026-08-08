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

# Canonical scoring schema, mirrored as Langfuse score configs by
# `ensure_score_configs()`. Keys: name, data_type, optional min/max/categories.
SCORE_CONFIGS: list[dict] = [
    {"name": "class_correct", "data_type": "BOOLEAN"},
    {"name": "stage_correct", "data_type": "BOOLEAN"},
    {"name": "parse_error", "data_type": "BOOLEAN"},
    {"name": "schema_valid", "data_type": "BOOLEAN"},
    {"name": "stage_completed", "data_type": "BOOLEAN"},
    {"name": "classification_confidence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "extraction_confidence", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
    {"name": "confidence_calibration_error", "data_type": "NUMERIC", "min_value": 0.0, "max_value": 1.0},
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
]


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
            if spec["name"] in existing:
                continue
            kwargs = {"name": spec["name"], "data_type": spec["data_type"]}
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
            created.append(spec["name"])
            logger.info("score_config_created", name=spec["name"], data_type=spec["data_type"])
    except Exception:
        logger.warning("score_config_creation_failed", exc_info=True)
    _configs_ensured.update(created)
    return created


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
) -> None:
    """Attach a score to a trace by id (offline/pilot scoring — no active
    tracing context required). No-ops when tracing is disabled."""
    client = _client()
    if client is None:
        return
    try:
        client.create_score(
            trace_id=trace_id,
            name=name,
            value=value,
            data_type=data_type,
            comment=comment,
            config_id=config_id,
            score_id=score_id,
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
        # No schema registered for this doc type — nothing to validate against.
        parsed["schema_valid"] = True
        return parsed
    try:
        model.model_validate(extracted_data)
        parsed["schema_valid"] = True
    except Exception:
        parsed["schema_valid"] = False
    return parsed


def emit_pipeline_scores(state: dict) -> dict:
    """Attach self-evident production scores for a finished run (no ground
    truth required). Called from `graph/build_graph.py:run_pipeline`. Returns
    the computed scores so callers can persist them locally too."""
    if not is_enabled():
        return {}

    stage = state.get("stage")
    extracted = state.get("extracted_data") or {}
    checks = validate_extraction(state.get("doc_type"), extracted)
    scores = {
        "parse_error": int(checks["parse_error"]),
        "schema_valid": int(checks["schema_valid"]),
        "stage_completed": int(stage == "archived"),
    }
    score_trace("parse_error", scores["parse_error"], data_type="BOOLEAN")
    score_trace("schema_valid", scores["schema_valid"], data_type="BOOLEAN")
    score_trace("stage_completed", scores["stage_completed"], data_type="BOOLEAN")

    cls_conf = state.get("classification_confidence")
    if isinstance(cls_conf, (int, float)) and not isinstance(cls_conf, bool):
        scores["classification_confidence"] = float(cls_conf)
        score_trace("classification_confidence", float(cls_conf), data_type="NUMERIC")
    ext_conf = state.get("extraction_confidence")
    if isinstance(ext_conf, (int, float)) and not isinstance(ext_conf, bool):
        scores["extraction_confidence"] = float(ext_conf)
        score_trace("extraction_confidence", float(ext_conf), data_type="NUMERIC")
    return scores
