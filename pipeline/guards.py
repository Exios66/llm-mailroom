"""Guardrails for agent outputs.

Agents are LLMs — they can return junk even when the provider call succeeds.
These guards are the deterministic safety net between an agent's raw output and
the pipeline's routing decisions. They never call an LLM; they validate
structure and values against the task specification.

Enforcement points (graph/build_graph.py):

- `classify_node` / `retry_classify_node`: `guard_classification` clamps
  out-of-range confidence and flags unknown doc types (routing already sends
  unknown types to review).
- `extract_node` / `retry_extract_node`: `guard_extraction` clamps confidence
  below the routing threshold when the extraction fails JSON parsing or schema
  validation, forcing the retry/review path instead of trusting bad output.

Each triggered guard is logged and recorded on the state as
`extraction_guardrail` / `classification_guardrail` (and scored as
`guardrail_triggered` by observability/scores.py).
"""

import structlog

logger = structlog.get_logger(__name__)

# Confidence clamp applied when a guardrail fires — below the `confidence.low`
# routing threshold, so the document is forced through retry → review instead of
# silently continuing with garbage output.
_GUARD_CONFIDENCE_CEILING = 0.5


def _is_valid_confidence(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0


def _valid_subtypes() -> frozenset[str]:
    """Valid contract-subtype keys (vendored LangChain sorter's 25 CUAD
    families + "other"). Lazily imported so guards never hard-pin the list."""
    try:
        from langchain_agents.sorter_agent import CONTRACT_SUBTYPE_KEYS, SUBTYPE_UNKNOWN

        return frozenset(CONTRACT_SUBTYPE_KEYS + [SUBTYPE_UNKNOWN])
    except Exception:
        return frozenset()


def guard_classification(state: dict) -> dict:
    """Validate the sorter's output. Returns {"ok", "issues", "confidence"}."""
    doc_type = state.get("doc_type")
    confidence = state.get("classification_confidence")
    contract_subtype = state.get("contract_subtype")
    issues: list[str] = []

    from pipeline.config import get_all_doc_types

    if doc_type not in get_all_doc_types():
        issues.append(f"unknown_doc_type: {doc_type!r} not in taxonomy")
    if confidence is not None and not _is_valid_confidence(confidence):
        issues.append(f"classification_confidence_out_of_range: {confidence!r}")
    # Contract-subtype discipline (mirrors the vendored sorter's key rules):
    # a contract MUST carry a valid subtype key; a non-contract MUST NOT.
    if doc_type == "contract":
        if not contract_subtype:
            issues.append("contract_subtype_missing")
        elif contract_subtype not in _valid_subtypes():
            issues.append(f"contract_subtype_unknown: {contract_subtype!r}")
    elif contract_subtype is not None:
        issues.append(
            f"contract_subtype_not_null_for_non_contract: {contract_subtype!r}"
        )

    result = {"ok": not issues, "issues": issues}
    if confidence is not None and _is_valid_confidence(confidence):
        result["confidence"] = float(confidence)
    return result


def guard_extraction(doc_type: str, extracted_data: dict | None) -> dict:
    """Validate a specialist's extraction against its schema.

    Returns {"ok", "issues", "parse_error", "schema_valid"}. `ok` is False when
    the extraction cannot be trusted (JSON parse failure or schema violation).
    """
    from observability.scores import validate_extraction

    extracted_data = extracted_data or {}
    checks = validate_extraction(doc_type, extracted_data)
    issues: list[str] = []
    if checks["parse_error"]:
        issues.append("extraction_parse_error")
    if not checks["schema_valid"]:
        issues.append("extraction_schema_invalid")
    return {
        "ok": not issues,
        "issues": issues,
        "parse_error": checks["parse_error"],
        "schema_valid": checks["schema_valid"],
    }


def apply_extraction_guard(
    doc_type: str,
    extracted_data: dict | None,
    confidence,
    *,
    attempts: int,
) -> tuple[dict, float | None]:
    """Run the extraction guardrail and return (guard_result, confidence).

    Confidence is clamped to `_GUARD_CONFIDENCE_CEILING` when the guard fires,
    so routing sends the document to retry/review instead of trusting bad
    output.
    """
    guard = guard_extraction(doc_type, extracted_data)
    new_confidence = confidence
    if not guard["ok"]:
        if _is_valid_confidence(confidence):
            new_confidence = min(float(confidence), _GUARD_CONFIDENCE_CEILING)
        elif confidence is None:
            new_confidence = 0.0
        else:
            new_confidence = _GUARD_CONFIDENCE_CEILING
        logger.warning(
            "extraction_guardrail_triggered",
            doc_type=doc_type,
            issues=guard["issues"],
            confidence_before=confidence,
            confidence_after=new_confidence,
            attempts=attempts,
        )
    return guard, new_confidence
