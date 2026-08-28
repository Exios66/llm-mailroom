"""Classify pipeline crashes so aborted runs are distinguishable.

``run_pipeline`` used to park every unexpected exception as
``escalation_reason="unexpected error"``. OpenRouter timeouts, 401s, and
corrupt files then looked identical in the catalog / visualizer.
"""

from __future__ import annotations

from typing import Any

LLM_TIMEOUT = "llm_timeout"
LLM_AUTH = "llm_auth"
LLM_RATE_LIMIT = "llm_rate_limit"
LLM_TRANSIENT = "llm_transient"
IO_ERROR = "io_error"
SCHEMA_ERROR = "schema_error"
RUN_BUDGET = "run_budget"
UNEXPECTED = "unexpected"

_AUTH_MARKERS = ("api key", "apikey", "unauthorized", "authentication", "invalid token")
_RATE_MARKERS = ("rate limit", "too many requests", "429")
_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded")
_SCHEMA_MARKERS = ("validation error", "schema", "pydantic")


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def classify_run_failure(exc: BaseException) -> dict[str, str]:
    """Return ``failure_class``, ``reason``, and ``detail`` for an abort."""
    from pipeline.limits import RunBudgetExceeded, RunDeadlineExceeded

    name = type(exc).__name__
    detail = str(exc)[:300]
    low = f"{name} {detail}".lower()
    status = _status_code(exc)

    if isinstance(exc, (RunDeadlineExceeded, RunBudgetExceeded)):
        return {
            "failure_class": RUN_BUDGET,
            "reason": f"{name}: {detail}" if detail else name,
            "detail": detail,
        }
    if isinstance(exc, (PermissionError, FileNotFoundError, IsADirectoryError)):
        return {"failure_class": IO_ERROR, "reason": f"{name}: {detail}", "detail": detail}
    if isinstance(exc, TimeoutError):
        return {"failure_class": LLM_TIMEOUT, "reason": f"{name}: {detail}", "detail": detail}
    if isinstance(exc, OSError):
        return {"failure_class": IO_ERROR, "reason": f"{name}: {detail}", "detail": detail}

    if status in (401, 403) or any(m in low for m in _AUTH_MARKERS):
        return {"failure_class": LLM_AUTH, "reason": f"{name}: {detail}", "detail": detail}
    if status == 429 or any(m in low for m in _RATE_MARKERS):
        return {
            "failure_class": LLM_RATE_LIMIT,
            "reason": f"{name}: {detail}",
            "detail": detail,
        }
    if status in (408, 504) or any(m in low for m in _TIMEOUT_MARKERS):
        return {"failure_class": LLM_TIMEOUT, "reason": f"{name}: {detail}", "detail": detail}
    if any(m in low for m in _SCHEMA_MARKERS):
        return {"failure_class": SCHEMA_ERROR, "reason": f"{name}: {detail}", "detail": detail}

    try:
        from llm.retry import is_transient_error

        if is_transient_error(exc):
            return {
                "failure_class": LLM_TRANSIENT,
                "reason": f"{name}: {detail}",
                "detail": detail,
            }
    except Exception:
        pass

    return {
        "failure_class": UNEXPECTED,
        "reason": f"{name}: {detail}" if detail else name,
        "detail": detail,
    }


def failure_audit_detail(classified: dict[str, str]) -> dict[str, Any]:
    return {
        "failure_class": classified.get("failure_class") or UNEXPECTED,
        "reason": classified.get("reason") or "unexpected error",
        "detail": classified.get("detail") or "",
    }
