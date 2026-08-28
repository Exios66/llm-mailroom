"""Forensic follow-up: SCORE_CONFIGS vs dojo 0.12.1, failure classes."""

import time

from llm_dojo_scoring import load_registry
from observability.scores import SCORE_CONFIGS
from pipeline.failures import (
    IO_ERROR,
    LLM_AUTH,
    LLM_RATE_LIMIT,
    LLM_TIMEOUT,
    RUN_BUDGET,
    UNEXPECTED,
    classify_run_failure,
)

# Names a forensic report claimed were "removed from dojo v0.12.1". They
# remain registered — this list is a tripwire against a real future drop.
_FORENSIC_CLAIMED_MISSING = (
    "content_topic_accuracy",
    "content_topic_f1_macro",
    "sentiment_accuracy",
    "sentiment_f1_macro",
    "maud_question_accuracy",
    "maud_question_macro_accuracy",
    "maud_clause_presence",
    "maud_valid_class_rate",
    "maud_category_accuracy",
    "intake_prep_completeness",
    "intake_changed_rate",
    "intake_messy_rate",
    "intake_hyphen_unwraps",
    "intake_collapsed_blanks",
    "extraction_precision",
    "extraction_recall",
    "extraction_f1",
    "extraction_f2",
    "entity_list_f1",
    "determination_consistency",
    "amount_exactness",
)


def test_forensic_claimed_missing_scores_are_in_dojo_registry():
    reg = load_registry()
    names = {c["name"] for c in SCORE_CONFIGS}
    missing = [n for n in _FORENSIC_CLAIMED_MISSING if n not in reg.metrics]
    assert missing == [], missing
    unlisted = [n for n in _FORENSIC_CLAIMED_MISSING if n not in names]
    assert unlisted == [], unlisted


def test_classify_auth_timeout_io_and_budget():
    class _Http(Exception):
        def __init__(self, status_code, msg):
            super().__init__(msg)
            self.status_code = status_code

    assert classify_run_failure(_Http(401, "invalid api key"))["failure_class"] == LLM_AUTH
    assert classify_run_failure(_Http(429, "rate limit"))["failure_class"] == LLM_RATE_LIMIT
    assert classify_run_failure(TimeoutError("openrouter timed out"))["failure_class"] == LLM_TIMEOUT
    assert classify_run_failure(PermissionError("/data/inbox"))["failure_class"] == IO_ERROR
    assert classify_run_failure(FileNotFoundError("missing.pdf"))["failure_class"] == IO_ERROR
    assert classify_run_failure(RuntimeError("boom"))["failure_class"] == UNEXPECTED

    from pipeline.limits import RunDeadlineExceeded

    classified = classify_run_failure(RunDeadlineExceeded(time.time() - 1))
    assert classified["failure_class"] == RUN_BUDGET
