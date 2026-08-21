import structlog
from typing import Any, Literal, Mapping

from pipeline.config import get_confidence_thresholds, get_all_doc_types
from observability.scores import validate_extraction

logger = structlog.get_logger(__name__)

# How many times a node may retry itself after a TRANSIENT provider error
# (connection error/timeout/rate-limit/5xx — see llm/retry.is_transient_error)
# before the document is sent to human review. Transient retries do NOT consume
# the confidence-based retry budget (classification_attempts/extraction_attempts).
_TRANSIENT_MAX_RETRIES = 2


def _transient_decision(state: dict, *, retry_target: str) -> Literal["retry", "human_review"]:
    """Route a transient-error flag: retry the same node up to
    `_TRANSIENT_MAX_RETRIES`, then human review.

    L-13: the transient budget is PER NODE — classify failures must not
    exhaust the counter that extract later needs. Each node tracks its own
    `transient_retries_<node>` key, reset on success.

    The node keeps `classification_attempts`/`extraction_attempts` unchanged on
    transient failures, so a flaky provider never burns the confidence retry
    budget (which is reserved for genuinely low-quality model output).
    """
    counter_key = f"transient_retries_{retry_target}"
    retries = state.get(counter_key, 0)
    if retries <= _TRANSIENT_MAX_RETRIES:
        logger.warning(
            "transient_retry",
            retry_target=retry_target,
            retries=retries,
            error=state.get("error_message"),
        )
        return "retry"
    logger.warning(
        "transient_retries_exhausted",
        retry_target=retry_target,
        retries=retries,
        doc_id=state.get("doc_id"),
    )
    return "human_review"


def after_classify(state: dict) -> Literal["classify", "retry_classify", "extract", "human_review"]:
    if state.get("transient_error"):
        if _transient_decision(state, retry_target="classify") == "retry":
            return "classify"
        return "human_review"

    confidence = state.get("classification_confidence")
    attempts = state.get("classification_attempts", 0)
    doc_type = state.get("doc_type")
    thresholds = get_confidence_thresholds()
    low = thresholds.get("low", 0.70)
    high = thresholds.get("high", 0.95)
    retry_max = thresholds.get("retry_max", 1)
    valid_types = get_all_doc_types()

    if doc_type and doc_type not in valid_types:
        logger.warning("unknown_doc_type", doc_type=doc_type)
        return "human_review"

    # High confidence: clearly matches one class -> auto-continue to extraction.
    if confidence is not None and confidence >= high:
        return "extract"

    # Medium band (low <= confidence < high): classified, but the model is not
    # clearly confident (e.g. multi-topic/ambiguous documents whose form still
    # fits a class). L-9: run ONE re-classification pass first — the retry
    # prompt ("re-evaluate") often resolves medium-confidence ambiguity — then
    # route to human review if it stays medium. Previously the band went
    # straight to review, skipping the retry budget entirely.
    if confidence is not None and confidence >= low:
        if attempts <= retry_max:
            logger.info(
                "medium_confidence_retry",
                confidence=confidence,
                attempts=attempts,
                doc_type=doc_type,
            )
            return "retry_classify"
        logger.info(
            "medium_confidence_review",
            confidence=confidence,
            attempts=attempts,
            doc_type=doc_type,
        )
        return "human_review"

    if attempts <= retry_max:
        logger.info("low_confidence_retry", confidence=confidence, attempts=attempts)
        return "retry_classify"

    logger.info("low_confidence_review", confidence=confidence, attempts=attempts)
    return "human_review"


def after_retry_classify(state: dict) -> Literal[
    "classify", "review_classify", "extract", "human_review"
]:
    if state.get("transient_error"):
        if _transient_decision(state, retry_target="classify") == "retry":
            return "classify"
        return "human_review"
    # KANBAN-062 (Lane A): a medium-band classification that survived the
    # retry now goes to the agent second opinion instead of straight to a
    # human.
    confidence = state.get("classification_confidence")
    thresholds = get_confidence_thresholds()
    low = thresholds.get("low", 0.70)
    high = thresholds.get("high", 0.95)
    if confidence is not None and low <= confidence < high:
        logger.info(
            "medium_confidence_agent_review",
            confidence=confidence,
            doc_id=state.get("doc_id"),
        )
        return "review_classify"
    # Post-retry resolution (explicit instead of delegating to after_classify,
    # whose retry arms are unreachable once the budget is spent): unknown type
    # or still-low confidence escalates to humans; high confidence proceeds.
    doc_type = state.get("doc_type")
    if not doc_type or doc_type not in get_all_doc_types():
        logger.warning(
            "unknown_doc_type_post_retry",
            doc_id=state.get("doc_id"),
            doc_type=doc_type,
        )
        return "human_review"
    if confidence is not None and confidence >= high:
        return "extract"
    return "human_review"


def after_extraction(state: dict) -> Literal[
    "extract", "retry_extract", "compile_report", "human_review", "boss_escalation"
]:
    if state.get("transient_error"):
        if _transient_decision(state, retry_target="extract") == "retry":
            return "extract"
        return "human_review"

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


def after_retry_extraction(state: dict) -> Literal["extract", "compile_report", "human_review", "boss_escalation"]:
    if state.get("transient_error"):
        if _transient_decision(state, retry_target="extract") == "retry":
            return "extract"
        return "human_review"
    return after_extraction(state)


def judge_gate(state: Mapping[str, Any]) -> bool:
    """KANBAN-063 cost contract: the judge is an EXCEPTION path, not a stage.

    Fires only when the extraction landed in the ambiguous band
    (low <= confidence < judge_band_high, default 0.85) — documents a human
    would want double-checked anyway. Clean high-confidence extractions skip
    with zero added LLM calls. Disable the whole lane with
    MAILROOM_JUDGE_VERIFY=off.
    """
    import os

    if os.environ.get("MAILROOM_JUDGE_VERIFY", "on").lower() in ("off", "false", "0", "no"):
        return False
    confidence = state.get("extraction_confidence")
    thresholds = get_confidence_thresholds()
    low = thresholds.get("low", 0.70)
    band_high = float(thresholds.get("judge_band_high", 0.85))
    return confidence is not None and low <= confidence < band_high


def after_extraction_gated(state: dict) -> Literal[
    "extract", "retry_extract", "compile_report", "judge_verify", "human_review", "boss_escalation"
]:
    """KANBAN-063 cost-contract wrapper around ``after_extraction``.

    Identical decisions EXCEPT a ``compile_report`` destination detours
    through ``judge_verify`` when the extraction landed in the ambiguous
    band (``judge_gate``). Clean runs keep today's path verbatim — zero
    added LLM calls. Conditional-edge maps are static, so the gate must be
    evaluated here at routing time, never in the edge map itself.
    """
    dest = after_extraction(state)
    if dest == "compile_report" and judge_gate(state):
        logger.info(
            "judge_gate_engaged",
            doc_id=state.get("doc_id"),
            extraction_confidence=state.get("extraction_confidence"),
        )
        return "judge_verify"
    return dest


def after_retry_extraction_gated(state: dict) -> Literal[
    "extract", "retry_extract", "compile_report", "judge_verify", "human_review", "boss_escalation"
]:
    """Same gate applied to the post-retry extraction router."""
    dest = after_retry_extraction(state)
    if dest == "compile_report" and judge_gate(state):
        logger.info(
            "judge_gate_engaged",
            doc_id=state.get("doc_id"),
            extraction_confidence=state.get("extraction_confidence"),
        )
        return "judge_verify"
    return dest


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


def after_review_classify(state: dict) -> Literal["review_classify", "extract", "human_review"]:
    """KANBAN-062 (Lane A) outcome router.

    The reviewer's high-confidence label wins (extract — with the reviewer's
    type applied by the node); anything else (reviewer unsure, labels
    conflicting at low confidence, or reviewer confirming genuine ambiguity)
    escalates to human review with BOTH opinions recorded on state. The lane
    is strictly fail-safe: every path it can take existed before the lane.
    Transient provider errors self-loop on the review node's OWN per-node
    budget (L-13) before escalating.
    """
    if state.get("transient_error"):
        if _transient_decision(state, retry_target="review_classify") == "retry":
            return "review_classify"
        return "human_review"
    verdict = state.get("review_verdict")
    confidence = state.get("reviewer_confidence")
    doc_type = state.get("reviewer_doc_type")
    thresholds = get_confidence_thresholds()
    high = thresholds.get("high", 0.95)
    if (
        verdict in ("reviewer_overrides", "reviewer_agrees_high")
        and confidence is not None
        and confidence >= high
        and doc_type in get_all_doc_types()
    ):
        logger.info(
            "review_classify_accepted",
            verdict=verdict,
            confidence=confidence,
            doc_type=doc_type,
            doc_id=state.get("doc_id"),
        )
        return "extract"
    logger.info(
        "review_classify_escalated",
        verdict=verdict,
        confidence=confidence,
        doc_id=state.get("doc_id"),
    )
    return "human_review"


def after_judge(state: dict) -> Literal["judge_verify", "compile_report", "arbiter", "human_review"]:
    """KANBAN-063 (Lane B): judge outcome router.

    ``complete`` (or a skipped/gated-out pass-through) proceeds; ``partial``/
    ``incomplete`` goes to the arbiter. Transient judge errors self-loop on
    the judge's OWN per-node budget (L-13) before escalating; a judge that
    hard-fails after the gate flagged the doc escalates to humans (fail-safe:
    the flag said this doc needs scrutiny).
    """
    if state.get("transient_error"):
        if _transient_decision(state, retry_target="judge_verify") == "retry":
            return "judge_verify"
        return "human_review"
    verdict = state.get("judge_verdict")
    if verdict in ("judge_error",):
        return "human_review"
    if verdict in (None, "", "skipped", "complete"):
        return "compile_report"
    return "arbiter"


def after_arbiter(state: dict) -> Literal["compile_report", "retry_extract", "human_review"]:
    """KANBAN-063 (Lane B): arbiter outcome router, with the retry bound.

    ``accept_with_caveats`` proceeds; ``retry_extraction`` re-runs extraction
    exactly once per document (the bound — compounding arbitration loops are
    the failure mode this prevents); anything else (or a retry demand past
    the bound) escalates to human review with the handoff summary attached.
    """
    decision = state.get("arbiter_decision")
    if decision == "accept_with_caveats":
        return "compile_report"
    if decision == "retry_extraction" and state.get("arbiter_retry_count", 0) < 1:
        logger.info(
            "arbiter_retry_approved",
            doc_id=state.get("doc_id"),
            fields=state.get("judge_findings"),
        )
        return "retry_extract"
    logger.info(
        "arbiter_escalated",
        decision=decision,
        doc_id=state.get("doc_id"),
    )
    return "human_review"
