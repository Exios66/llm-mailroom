"""Run-limit enforcement for document runs.

Two hard stop mechanisms protect credit spend:

- **Wall-clock deadline** (`RunDeadlineExceeded`): a run must finish within
  `run_limits.deadline_seconds` (default 1h). The deadline is stamped into the
  graph state at run start and checked at every node boundary and before each
  LLM retry attempt.
- **Token budget** (`RunBudgetExceeded`): every LLM call's `usage` is recorded
  into a contextvar accumulator for the current run; the node guard aborts the
  run once cumulative output tokens pass `run_limits.max_total_output_tokens`
  (stuck/excessively-verbose model guard).

Both are cooperative (no thread killing) but airtight in practice: the
per-call timeout (`llm_call_timeout_seconds`) bounds any single hanging call,
and the checks run between nodes and inside the retry loop.
"""

import contextvars
import time
import structlog

logger = structlog.get_logger(__name__)


class RunDeadlineExceeded(Exception):
    """Raised when a document run passes its wall-clock deadline."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        super().__init__(f"run deadline exceeded (deadline {time.ctime(deadline)})")


class RunBudgetExceeded(Exception):
    """Raised when a run's cumulative output tokens pass the per-run cap."""

    def __init__(self, used: int, cap: int):
        self.used = used
        self.cap = cap
        super().__init__(f"run output-token budget exceeded: {used} >= {cap}")


# No shared default: a call made outside an active run (Gmail triage, relations
# scan threads, ad-hoc scripts) has no accumulator and records nothing, so the
# prompts/completions kept for the audit visit can never pile up in a
# module-level list. ``reset_run_usage()`` opens the accumulator for a run.
_run_usage: contextvars.ContextVar[list | None] = contextvars.ContextVar("run_usage", default=None)
_run_deadline: contextvars.ContextVar = contextvars.ContextVar("run_deadline", default=None)
_run_started: contextvars.ContextVar = contextvars.ContextVar("run_started", default=None)


#: Which traced-node visit the current context is running (set by ``audit.trace.begin_visit``). Calls are
#: stamped with it, so usage that arrives late (an abandoned, timed-out call) is attributed to the visit
#: that made it, not to whatever node runs when it lands. Run-level totals and budgets ignore it.
_visit_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("visit_scope", default=None)


def set_visit_scope(scope: str | None) -> contextvars.Token:
    return _visit_scope.set(scope)


def reset_visit_scope(token: contextvars.Token) -> None:
    _visit_scope.reset(token)


def get_visit_scope() -> str | None:
    return _visit_scope.get()


def reset_run_usage() -> None:
    """Start a fresh accumulator for the current run (called per _execute_run)."""
    _run_usage.set([])


def set_run_deadline(deadline: float | None) -> None:
    """Stamp the current run's wall-clock deadline (called per _execute_run)."""
    _run_deadline.set(deadline)


def set_run_started(started_at: float | None) -> None:
    """Wall-clock start of the current ``run_pipeline`` invocation (for the audit row's metrics)."""
    _run_started.set(started_at)


def get_run_started() -> float | None:
    """Return the current run's wall-clock start, or ``None`` before it is set."""
    return _run_started.get()


def get_run_deadline() -> float | None:
    return _run_deadline.get()


def _token_count(value) -> int | None:
    """A valid token count as an ``int``, else ``None``.

    Accepts ints and integral floats (JSON usage often decodes ``128`` as
    ``128.0``). Rejects bools (an ``int`` subclass), negatives, NaN/inf and
    non-integral floats.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():  # False for NaN and +/-inf too
            return None
        value = int(value)
    if not isinstance(value, int) or value < 0:
        return None
    return value


def record_usage(
    usage,
    model: str | None = None,
    agent: str | None = None,
    served_model: str | None = None,
    *,
    prompt: str | None = None,
    completion: str | None = None,
    prompt_version: str | int | None = None,
) -> None:
    """Append an OpenAI-compatible usage object to the current run's
    accumulator. Tolerates mocks and odd shapes: only real non-negative integer
    counts count (integral floats such as ``128.0`` are converted to int).

    Accepts SDK ``CompletionUsage`` objects (``usage.prompt_tokens``) and
    LangChain ``usage_metadata`` dicts (``usage["input_tokens"]`` /
    ``usage["prompt_tokens"]``) so the vendored LangChain agents record too.
    ``agent`` attributes the call to the calling agent (per-agent token/cost
    accounting in evals and run reports); callers pass ``self.agent_name``.
    ``served_model`` is the model the provider reports having answered with
    (``response.model``) — it differs from ``model`` behind a router such as
    ``openrouter/free`` or a ``models`` fallback chain.

    ``prompt``, ``completion`` and ``prompt_version`` are kept for the node's
    audit visit (``audit.trace``, spec §4.4); callers that have them pass them.
    """
    if usage is None:
        return
    if isinstance(usage, dict):
        # A real zero is valid; fall back only for absent/None counts.
        prompt_tokens = usage.get("input_tokens")
        if prompt_tokens is None:
            prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("output_tokens")
        if completion_tokens is None:
            completion_tokens = usage.get("completion_tokens")
    else:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
    prompt_n = _token_count(prompt_tokens)
    completion_n = _token_count(completion_tokens)
    if prompt_n is None or completion_n is None:
        # Mocks, None and non-numeric shapes are expected and dropped quietly;
        # a real number that is not a valid count (1.5, NaN, -1, True) is a
        # provider/shape bug worth a log line (#188).
        if any(isinstance(v, (int, float)) for v in (prompt_tokens, completion_tokens)):
            logger.warning(
                "usage_dropped_invalid_counts",
                prompt_tokens=repr(prompt_tokens),
                completion_tokens=repr(completion_tokens),
                model=model,
                agent=agent,
            )
        return
    accumulator = _run_usage.get()
    if accumulator is None:
        # Outside an active run: nothing reads this record, so keep nothing
        # (and never retain prompt/completion text).
        return
    accumulator.append(
        {
            "prompt_tokens": prompt_n,
            "completion_tokens": completion_n,
            "model": model,
            "agent": agent,
            "served_model": served_model if isinstance(served_model, str) and served_model else None,
            "prompt": prompt,
            "completion": completion,
            "prompt_version": prompt_version,
            "visit_scope": _visit_scope.get(),
        }
    )


def run_calls() -> list[dict]:
    """Return the current run's mutable LLM-call accumulator, oldest first.

    Outside an active run this is a fresh empty list (appending to it is lost).
    """
    accumulator = _run_usage.get()
    return accumulator if accumulator is not None else []


def call_cost_usd(call: dict) -> float | None:
    """USD cost of one recorded call from ``cost_models``; ``None`` when the model has no price."""
    price = _price_for(call.get("model"))
    if not price:
        return None
    return (call["prompt_tokens"] / 1e6) * float(price.get("input_per_million", 0)) + (
        call["completion_tokens"] / 1e6
    ) * float(price.get("output_per_million", 0))


def usage_mark() -> int:
    """Position in the current run's usage log; pair with :func:`usage_since`."""
    return len(run_calls())


def usage_since(mark: int, agent: str | None = None) -> dict:
    """Calls and tokens recorded since ``usage_mark`` (one agent's, or all when ``agent`` is None).

    Lets a multi-call step (the judge's windows) report what it alone cost without
    resetting the run-wide accumulator the token budget reads.
    """
    items = [i for i in run_calls()[mark:] if agent is None or i.get("agent") == agent]
    prompt = sum(i["prompt_tokens"] for i in items)
    completion = sum(i["completion_tokens"] for i in items)
    return {"calls": len(items), "prompt_tokens": prompt, "completion_tokens": completion}


def usage_summary() -> dict:
    """Aggregate the current run's recorded usage.

    Returns {"prompt_tokens", "completion_tokens", "total", "calls",
    "by_agent"} where ``by_agent`` maps agent name → per-agent {calls,
    prompt_tokens, completion_tokens, total, models, served_models} (unattributed calls
    land under ``None`` → key ``"unattributed"``).
    """
    items = _run_usage.get() or []
    prompt = sum(i["prompt_tokens"] for i in items)
    completion = sum(i["completion_tokens"] for i in items)
    by_agent: dict[str, dict] = {}
    for i in items:
        key = i.get("agent") or "unattributed"
        slot = by_agent.setdefault(
            key,
            {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total": 0,
                "models": [],
                "served_models": [],
            },
        )
        slot["calls"] += 1
        slot["prompt_tokens"] += i["prompt_tokens"]
        slot["completion_tokens"] += i["completion_tokens"]
        slot["total"] += i["prompt_tokens"] + i["completion_tokens"]
        model = i.get("model")
        if model and model not in slot["models"]:
            slot["models"].append(model)
        served = i.get("served_model")
        if served and served not in slot["served_models"]:
            slot["served_models"].append(served)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total": prompt + completion,
        "calls": len(items),
        "by_agent": by_agent,
    }


def get_run_limits() -> dict:
    from pipeline.config import load_config

    try:
        return load_config().get("run_limits", {}) or {}
    except Exception:
        return {}


def get_deadline_seconds() -> int:
    return int(get_run_limits().get("deadline_seconds", 3600))


def get_call_timeout_seconds() -> int:
    return int(get_run_limits().get("llm_call_timeout_seconds", 120))


def get_max_total_output_tokens() -> int:
    return int(get_run_limits().get("max_total_output_tokens", 20000))


def check_run_deadline(deadline: float | None) -> None:
    """Raise `RunDeadlineExceeded` if the wall clock has passed the deadline."""
    if deadline is not None and time.time() >= float(deadline):
        raise RunDeadlineExceeded(float(deadline))


def check_token_budget() -> None:
    """Raise `RunBudgetExceeded` if cumulative *output* tokens pass the cap.

    Only completion tokens count (the cap's documented purpose is to abort
    stuck/excessively-verbose models that keep generating). Prompt tokens are
    excluded: large documents legitimately consume tens of thousands of input
    tokens per call, so counting them would false-positive on real work.
    """
    used = usage_summary()["completion_tokens"]
    cap = get_max_total_output_tokens()
    if used >= cap:
        raise RunBudgetExceeded(used, cap)


def _price_for(model: str | None) -> dict | None:
    if not model:
        return None
    from pipeline.config import load_config

    try:
        models = load_config().get("cost_models", {}) or {}
    except Exception:
        return None
    spec = models.get(model)
    if not isinstance(spec, dict):
        return None
    return spec


def estimate_cost() -> float:
    """Estimate USD cost for the current run from recorded usage and the
    `cost_models` table. Unknown models contribute 0."""
    total = 0.0
    for call in _run_usage.get() or []:
        price = _price_for(call.get("model"))
        if not price:
            continue
        total += (call["prompt_tokens"] / 1e6) * float(price.get("input_per_million", 0))
        total += (call["completion_tokens"] / 1e6) * float(price.get("output_per_million", 0))
    return round(total, 4)
