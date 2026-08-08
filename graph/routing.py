import structlog
from typing import Literal

from pipeline.config import get_confidence_thresholds, get_all_doc_types
from observability.scores import validate_extraction

logger = structlog.get_logger(__name__)


def after_classify(state: dict) -> Literal["retry_classify", "extract", "human_review"]:
    confidence = state.get("classification_confidence")
    attempts = state.get("classification_attempts", 0)
    doc_type = state.get("doc_type")
    thresholds = get_confidence_thresholds()
    low = thresholds.get("low", 0.70)
    retry_max = thresholds.get("retry_max", 1)
    valid_types = get_all_doc_types()

    if doc_type and doc_type not in valid_types:
        logger.warning("unknown_doc_type", doc_type=doc_type)
        return "human_review"

    if confidence is not None and confidence >= low:
        return "extract"

    if attempts <= retry_max:
        logger.info("low_confidence_retry", confidence=confidence, attempts=attempts)
        return "retry_classify"

    logger.info("low_confidence_review", confidence=confidence, attempts=attempts)
    return "human_review"


def after_retry_classify(state: dict) -> Literal["extract", "human_review"]:
    return after_classify(state)


def after_extraction(state: dict) -> Literal[
    "retry_extract", "compile_report", "human_review", "boss_escalation"
]:
    confidence = state.get("extraction_confidence")
    attempts = state.get("extraction_attempts", 0)
    thresholds = get_confidence_thresholds()
    low = thresholds.get("low", 0.70)
    retry_max = thresholds.get("retry_max", 1)
    conflict = state.get("conflict_detected", False)

    if conflict:
        logger.info("conflict_escalation", doc_id=state.get("doc_id"))
        return "boss_escalation"

    # Schema gate: an extraction that fails the doc type's pydantic schema
    # (parse error, wrong types, fabricated shape) must never archive — retry
    # once, then human review, regardless of the model's stated confidence.
    # Only enforced when there is extraction data to validate (a None/empty
    # extraction is caught by the confidence path below).
    extracted = state.get("extracted_data")
    if extracted:
        checks = validate_extraction(state.get("doc_type"), extracted)
        if checks.get("schema_valid") is False:
            if attempts <= retry_max:
                logger.info(
                    "extraction_schema_invalid_retry",
                    doc_id=state.get("doc_id"),
                    attempts=attempts,
                )
                return "retry_extract"
            logger.info(
                "extraction_schema_invalid_review",
                doc_id=state.get("doc_id"),
                attempts=attempts,
            )
            return "human_review"

    if confidence is not None and confidence >= low:
        return "compile_report"

    if attempts <= retry_max:
        logger.info("extraction_retry", confidence=confidence, attempts=attempts)
        return "retry_extract"

    logger.info("extraction_review", confidence=confidence, attempts=attempts)
    return "human_review"


def after_retry_extraction(state: dict) -> Literal["compile_report", "human_review", "boss_escalation"]:
    return after_extraction(state)


def after_boss(state: dict) -> Literal["compile_report", "human_review"]:
    decision = state.get("review_decision")
    if decision == "approved":
        return "compile_report"
    return "human_review"


def after_human_review(state: dict) -> Literal["compile_report", "failed"]:
    decision = state.get("review_decision")
    if decision == "approved":
        return "compile_report"
    return "failed"
