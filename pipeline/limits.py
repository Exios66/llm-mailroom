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


_run_usage: contextvars.ContextVar[list] = contextvars.ContextVar("run_usage", default=[])
_run_deadline: contextvars.ContextVar = contextvars.ContextVar("run_deadline", default=None)


def reset_run_usage() -> None:
    """Start a fresh accumulator for the current run (called per _execute_run)."""
    _run_usage.set([])


def set_run_deadline(deadline: float | None) -> None:
    """Stamp the current run's wall-clock deadline (called per _execute_run)."""
    _run_deadline.set(deadline)


def get_run_deadline() -> float | None:
    return _run_deadline.get()


def record_usage(usage, model: str | None = None) -> None:
    """Append an OpenAI-compatible usage object to the current run's
    accumulator. Tolerates mocks and odd shapes: only real int counts count.

    Accepts SDK ``CompletionUsage`` objects (``usage.prompt_tokens``) and
    LangChain ``usage_metadata`` dicts (``usage["input_tokens"]`` /
    ``usage["prompt_tokens"]``) so the vendored LangChain agents record too.
    """
    if usage is None:
        return
    if isinstance(usage, dict):
        prompt = usage.get("input_tokens") or usage.get("prompt_tokens")
        completion = usage.get("output_tokens") or usage.get("completion_tokens")
    else:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return
    _run_usage.get().append(
        {"prompt_tokens": prompt, "completion_tokens": completion, "model": model}
    )


def usage_summary() -> dict:
    """Aggregate the current run's recorded usage.

    Returns {"prompt_tokens", "completion_tokens", "total", "calls"}.
    """
    items = _run_usage.get()
    prompt = sum(i["prompt_tokens"] for i in items)
    completion = sum(i["completion_tokens"] for i in items)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total": prompt + completion,
        "calls": len(items),
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
    for call in _run_usage.get():
        price = _price_for(call.get("model"))
        if not price:
            continue
        total += (call["prompt_tokens"] / 1e6) * float(price.get("input_per_million", 0))
        total += (call["completion_tokens"] / 1e6) * float(price.get("output_per_million", 0))
    return round(total, 4)
