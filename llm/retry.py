"""Transient-failure retry for LLM chat completions.

Wraps `client.chat.completions.create(...)` with retry-on-transient-failure
semantics. Only errors that are safe to retry are retried:

  - `openai.APIConnectionError`  (e.g. the `Connection error.` seen on
    `classify-document`/gpt-4o)
  - `openai.APITimeoutError`
  - `openai.RateLimitError`
  - `openai.APIStatusError` with `status >= 500` (server-side errors)

Client errors (4xx) and auth errors are never retried — with one narrow,
documented exception: Alibaba/Qwen's `json_object` gate. OpenRouter routes
`qwen/qwen3.7-flash` across multiple upstream providers; the Alibaba route
intermittently rejects requests that pass every other route with a 400
"messages must contain the word 'json'". Because the exact same messages
succeed on retry (proven in pilot traces), this specific 400 is treated as
retryable, bounded by `max_attempts`.

The OpenAI SDK's own internal retries (max_retries) still apply first;
this is an additional, visible, backoff layer with logging.

The Langfuse instrumentation intercepts `Completions.create`, so every attempt
is traced as its own generation.
"""

import time
import random
import structlog
from openai import APIConnectionError, APITimeoutError, RateLimitError, APIStatusError, BadRequestError

logger = structlog.get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Alibaba/Qwen json_object gate: the raw provider error (via OpenRouter) is a
# 400 whose message always contains this phrase. Bounded retry only for this
# exact quirk — never a blanket 4xx retry.
_JSON_MODE_400_MARKERS = ("must contain the word 'json'",)


def _is_json_mode_400(exc: Exception) -> bool:
    if not isinstance(exc, BadRequestError):
        return False
    try:
        text = str(exc)
    except Exception:
        return False
    return any(marker in text for marker in _JSON_MODE_400_MARKERS)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError):
        if exc.status_code in _RETRYABLE_STATUS:
            return True
        if exc.status_code == 400 and _is_json_mode_400(exc):
            return True
    return False


def is_transient_error(exc: Exception) -> bool:
    """Public predicate: should the caller retry this exception?

    Used by graph nodes to distinguish provider-side transient failures
    (retry the same node) from hard failures (crash or route to review).
    """
    return _is_retryable(exc)


def _retry_config() -> dict:
    try:
        from pipeline.config import load_config
        return load_config().get("llm_retry", {})
    except Exception:
        return {}


def retry_chat_completion(
    client,
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    jitter: float | None = None,
    timeout: float | None = None,
    run_deadline: float | None = None,
    **kwargs,
):
    """Call `client.chat.completions.create(**kwargs)`, retrying transient
    failures with exponential backoff + jitter.

    Tunables default to the `llm_retry:` section of taxonomy.yaml. `timeout`
    defaults to `run_limits.llm_call_timeout_seconds` and is passed to the SDK
    so a hanging provider request is bounded. When `run_deadline` is set, the
    wall-clock deadline is re-checked before every attempt, so a run whose time
    is up stops burning credits instead of starting another retry.
    Returns the SDK response on success, re-raises the last exception when all
    attempts are exhausted.
    """
    from pipeline.limits import check_run_deadline, get_call_timeout_seconds

    cfg = _retry_config()
    max_attempts = max_attempts if max_attempts is not None else int(cfg.get("max_attempts", 3))
    base_delay = base_delay if base_delay is not None else float(cfg.get("base_delay", 1.0))
    max_delay = max_delay if max_delay is not None else float(cfg.get("max_delay", 30.0))
    jitter = jitter if jitter is not None else float(cfg.get("jitter", 0.3))
    if timeout is None:
        timeout = float(get_call_timeout_seconds())
    attempt = 0
    while True:
        attempt += 1
        if run_deadline is not None:
            check_run_deadline(run_deadline)
        try:
            return client.chat.completions.create(**kwargs, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — we inspect and re-raise below
            if not _is_retryable(exc) or attempt >= max_attempts:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (1 + random.uniform(-jitter, jitter))
            logger.warning(
                "llm_retry",
                attempt=attempt,
                max_attempts=max_attempts,
                error_type=type(exc).__name__,
                detail=str(exc)[:300],
                retry_in_s=round(delay, 2),
                name=kwargs.get("name"),
            )
            time.sleep(max(0.0, delay))
