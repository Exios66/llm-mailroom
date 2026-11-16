"""Transient-failure retry for LLM chat completions.

Wraps `client.chat.completions.create(...)` with retry-on-transient-failure
semantics. Only errors that are safe to retry are retried:

  - `openai.APIConnectionError`  (e.g. the `Connection error.` seen on
    `classify-document`/gpt-4o)
  - `openai.APITimeoutError`
  - `openai.RateLimitError`
  - `openai.APIStatusError` with `status >= 500` (server-side errors)
  - `EmptyChoicesError`: an HTTP 200 whose body has no usable `choices`
    (OpenRouter answers this way when the upstream provider fails
    mid-generation, with an `error` body) — #260

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


class EmptyChoicesError(RuntimeError):
    """A 200 response that carries no usable completion (#260).

    OpenRouter returns HTTP 200 with ``choices: null`` and an ``error`` body
    when an upstream provider fails mid-generation. That is a provider-side
    transient failure, not a malformed request: it is retried like a 5xx and,
    once the budget is spent, reaches the nodes' transient path instead of
    crashing on ``choices[0]``.

    ``status_code`` carries the provider's error code when it is an int, so
    health recording and the rate-limit breaker see the real status.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


_MISSING = object()


def _provider_error(response) -> tuple[int | None, str]:
    """``(code, message)`` from a response's ``error`` body, tolerant of the
    SDK model (extra fields), plain dicts and objects."""
    err = getattr(response, "error", None)
    if err is None:
        extra = getattr(response, "model_extra", None)
        if isinstance(extra, dict):
            err = extra.get("error")
    if err is None:
        return None, ""
    if isinstance(err, dict):
        code, message = err.get("code"), err.get("message")
    else:
        code, message = getattr(err, "code", None), getattr(err, "message", None)
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    text = str(message) if message not in (None, "") else (str(err) if not isinstance(err, dict) else "")
    return code, text[:300]


def validate_completion(response) -> None:
    """Raise :class:`EmptyChoicesError` when ``response`` has no usable choice.

    Unusable means ``choices`` is ``None`` or an empty list/tuple, or the first
    choice has no ``message`` (absent or ``None``). Anything else (including
    objects without a ``choices`` attribute) is left to the caller.
    """
    choices = getattr(response, "choices", _MISSING)
    if choices is _MISSING:
        return
    problem = None
    if choices is None:
        problem = "choices is null"
    elif isinstance(choices, (list, tuple)):
        if not choices:
            problem = "choices is empty"
        elif getattr(choices[0], "message", None) is None:
            problem = "first choice has no message"
    if problem is None:
        return
    code, message = _provider_error(response)
    detail = f"provider error {code}: {message}" if code is not None else (
        f"provider error: {message}" if message else "no error body"
    )
    model = getattr(response, "model", None)
    model_text = f" model={model}" if isinstance(model, str) and model else ""
    raise EmptyChoicesError(
        f"LLM response had no usable completion ({problem}; {detail}){model_text}",
        status_code=code,
    )


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    if resp is not None:
        value = getattr(resp, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _retry_after_seconds(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        raw = None
    if raw in (None, ""):
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _is_modal_url(base_url: str | None) -> bool:
    return bool(base_url) and "modal.run" in str(base_url)


def _is_gateway_url(base_url: str | None) -> bool:
    """Whether the URL starts with the configured ``LITELLM_BASE_URL``.

    Ignore trailing slashes; an unset gateway or empty URL returns False.
    Matching URLs qualify for the gateway cold-start backoff on HTTP 503.
    """
    import os

    gateway = os.environ.get("LITELLM_BASE_URL", "").strip().rstrip("/")
    return bool(base_url) and bool(gateway) and str(base_url).rstrip("/").startswith(gateway)


def retry_sleep_seconds(
    exc: Exception, attempt: int, cfg: dict | None = None, base_url: str | None = None
) -> float:
    """Return nonnegative retry backoff in seconds; ``attempt`` is one-based.

    429s use the rate-limit delay and any numeric Retry-After value. A 503
    from a Modal URL or a URL matching ``LITELLM_BASE_URL`` uses the longer
    cold-start backoff (DMR-052). Both paths grow exponentially, cap the delay,
    then apply jitter, which can exceed the cap. An absent or empty ``cfg``
    uses the taxonomy retry settings.
    """
    cfg = cfg or _retry_config()
    base = float(cfg.get("base_delay", 1.0))
    max_delay = float(cfg.get("max_delay", 30.0))
    jitter = float(cfg.get("jitter", 0.3))
    if (_is_modal_url(base_url) or _is_gateway_url(base_url)) and _status_code(exc) == 503:
        cold = float(cfg.get("modal_cold_start_delay", 90.0))
        max_cold = float(cfg.get("modal_cold_start_max_delay", 240.0))
        delay = min(max_cold, cold * (2 ** max(0, attempt - 1)))
        return max(0.0, delay * (1 + random.uniform(-jitter, jitter)))
    rate_limited = isinstance(exc, RateLimitError) or _status_code(exc) == 429
    if rate_limited:
        base = float(cfg.get("rate_limit_base_delay", 8.0))
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            base = max(base, retry_after)
    delay = min(max_delay, base * (2 ** max(0, attempt - 1)))
    return max(0.0, delay * (1 + random.uniform(-jitter, jitter)))

# Alibaba/Qwen json_object gate: the raw provider error (via OpenRouter) is a
# 400 whose message always contains this phrase. Bounded retry only for this
# exact quirk — never a blanket 4xx retry.
_JSON_MODE_400_MARKERS = ("must contain the word 'json'",)

# Model-capability 400s (free-swarm failover): the provider rejects the
# request because THIS MODEL lacks the requested feature (live: ling via
# Novita — "does not support feature: structured-outputs"). A capability
# mismatch is a property of the MODEL, not of the request — the next swarm
# entry is the correct response. Never a blanket 4xx retry.
_CAPABILITY_400_MARKERS = ("does not support feature",)


def _is_json_mode_400(exc: Exception) -> bool:
    if not isinstance(exc, BadRequestError):
        return False
    try:
        text = str(exc)
    except Exception:
        return False
    return any(marker in text for marker in _JSON_MODE_400_MARKERS)


def _is_model_capability_error(exc: Exception) -> bool:
    """A 400 that is really a MODEL-capability mismatch (free-swarm failover
    trigger) — e.g. a provider rejecting the model for structured-outputs."""
    if not isinstance(exc, BadRequestError):
        return False
    try:
        text = str(exc)
    except Exception:
        return False
    return any(marker in text for marker in _CAPABILITY_400_MARKERS)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, EmptyChoicesError):
        # A provider code in the 200 body that names a client-side failure
        # (bad request, auth, payment, not found) will not heal on retry: fail
        # fast. No code, 408 or a retryable/5xx code is a transient upstream blip.
        code = exc.status_code
        return code is None or code == 408 or code in _RETRYABLE_STATUS or code >= 500
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


def _free_swarm() -> list[str]:
    """The ordered free-model fallback chain (taxonomy ``free_model_swarm:``).

    Empty when unconfigured — failover then never engages.
    """
    try:
        from pipeline.config import load_config

        return [str(m) for m in (load_config().get("free_model_swarm") or [])]
    except Exception:
        return []


def _ordered_dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_openrouter_url(base_url: str | None) -> bool:
    return bool(base_url) and "openrouter.ai" in str(base_url)


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

    FREE-SWARM FAILOVER (live-verified 2026-09-04): when the request's model
    is free and a rate-limit error arrives, the next taxonomy
    `free_model_swarm:` entry takes over for the following attempt — the
    OpenRouter shared free pool saturates for minutes at a time, and burning
    every attempt on one saturated model parked documents that another free
    model could have served instantly. Paid models NEVER rotate (a failover
    would silently change cost); the swarm order is priority (primary first).
    On an OpenRouter base URL the chain is sent as the ``models`` array (with
    ``provider.require_parameters``) so the server falls back instead.

    FREE-QUOTA BREAKER (``llm/quota.py``): free calls record 429s/successes;
    after ``free_quota.trip_after`` consecutive 429s further free calls raise
    ``FreeQuotaExhausted`` without touching the network until the cooldown
    ends. Paid models never touch the breaker.

    Returns the SDK response on success, re-raises the last exception when all
    attempts are exhausted.
    """
    from pipeline.limits import check_run_deadline, get_call_timeout_seconds

    cfg = _retry_config()
    max_attempts = max_attempts if max_attempts is not None else int(cfg.get("max_attempts", 5))
    if timeout is None:
        timeout = float(get_call_timeout_seconds())
    model = str(kwargs.get("model") or "")
    free = False
    if model:
        from .client import is_free_model

        free = is_free_model(model)
    failover = [m for m in _free_swarm() if m != model] if free else []
    breaker = None
    if free:
        from .quota import get_breaker

        breaker = get_breaker()
        base_url = str(getattr(client, "base_url", "") or "")
        chain = _ordered_dedup([model, *failover])
        if _is_openrouter_url(base_url) and len(chain) > 1:
            # Server-side fallback: OpenRouter tries `models` in order and
            # only routes to providers supporting every request parameter,
            # so the client-side rotation below is not needed.
            # `model` stays the primary; `models` carries only the fallbacks.
            extra = dict(kwargs.get("extra_body") or {})
            extra["models"] = chain[1:]
            provider = dict(extra.get("provider") or {})
            provider["require_parameters"] = True
            extra["provider"] = provider
            kwargs = {**kwargs, "extra_body": extra}
            failover = []
    from .provider_health import record_llm_outcome

    health_url = str(getattr(client, "base_url", "") or "")
    attempt = 0
    while True:
        attempt += 1
        if run_deadline is not None:
            check_run_deadline(run_deadline)
        if breaker is not None and breaker.is_open():
            from .quota import FreeQuotaExhausted

            logger.warning("llm_free_quota_open", model=kwargs.get("model"), open_until=breaker.open_until)
            raise FreeQuotaExhausted(breaker.open_until)
        started = time.monotonic()
        try:
            response = client.chat.completions.create(**kwargs, timeout=timeout)
            validate_completion(response)
            record_llm_outcome(health_url, kwargs.get("model"), ok=True, status=200,
                               latency_s=time.monotonic() - started)
            if breaker is not None:
                breaker.note_success()
            return response
        except Exception as exc:  # noqa: BLE001 — we inspect and re-raise below
            record_llm_outcome(health_url, kwargs.get("model"), ok=False, status=_status_code(exc),
                               latency_s=time.monotonic() - started)
            if breaker is not None and (
                isinstance(exc, RateLimitError) or _status_code(exc) == 429
            ):
                breaker.note_rate_limit(_retry_after_seconds(exc))
                if breaker.is_open():
                    # Raised even on the final attempt, so callers always see
                    # the breaker (and degrade) rather than a bare 429.
                    from .quota import FreeQuotaExhausted

                    logger.warning(
                        "llm_free_quota_open", model=kwargs.get("model"), open_until=breaker.open_until
                    )
                    raise FreeQuotaExhausted(breaker.open_until) from exc
            # A model-capability 400 is not "retryable" in place, but it IS a
            # failover trigger — the next swarm entry may support the feature.
            # With no failover model left, retrying the same model is futile.
            if (
                not _is_retryable(exc)
                and not (failover and _is_model_capability_error(exc))
            ) or attempt >= max_attempts:
                raise
            if failover and (
                isinstance(exc, RateLimitError) or _is_model_capability_error(exc)
            ):
                nxt = failover.pop(0)
                logger.warning(
                    "llm_free_failover",
                    from_model=kwargs.get("model"),
                    to_model=nxt,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                kwargs = {**kwargs, "model": nxt}
            delay = retry_sleep_seconds(
                exc,
                attempt,
                cfg,
                base_url=str(getattr(client, "base_url", "") or ""),
            )
            logger.warning(
                "llm_retry",
                attempt=attempt,
                max_attempts=max_attempts,
                error_type=type(exc).__name__,
                detail=str(exc)[:300],
                retry_in_s=round(delay, 2),
                name=kwargs.get("name"),
                model=kwargs.get("model"),
            )
            time.sleep(max(0.0, delay))
