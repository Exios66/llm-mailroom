"""Observability facade — picks the active tracing backend.

Backend selection (env `OBSERVABILITY_PROVIDER`):

  auto        (default) langfuse if LANGFUSE_SECRET_KEY is set, else braintrust
              if BRAINTRUST_API_KEY is set, else phoenix (local, free)
  langfuse    force Langfuse
  braintrust  force Braintrust
  phoenix     force Arize Phoenix (local OpenTelemetry, cost-free)
  none        disable tracing entirely

The ``auto`` chain is aligned with ``llm-entity-extraction``'s architecture:
cloud backends (Langfuse/Braintrust) win when their keys are set, otherwise
tracing falls through to the **local, cost-free Arize Phoenix** backend instead
of silently disabling. This keeps every LLM call traced with zero spend on top
of the API calls.

Two integration points:

- `instrument_openai_client` wraps the OpenAI client built in
  `llm/client.py:get_llm`, so every LLM call becomes a traced generation.
- `pipeline_trace` / `traced_node` add structured, nested observations around
  document runs and graph nodes (Langfuse-only structured spans; the
  Phoenix/Braintrust backends still trace LLM calls). All helpers no-op safely
  when tracing is disabled.
"""

import functools
import os
import structlog
from contextlib import contextmanager

logger = structlog.get_logger(__name__)


def resolve_provider_name() -> str:
    """Return one of: 'langfuse', 'braintrust', 'phoenix', 'none'."""
    choice = os.environ.get("OBSERVABILITY_PROVIDER", "auto").strip().lower()

    if choice in ("langfuse", "braintrust", "phoenix", "none"):
        return choice

    # auto: prefer langfuse, then braintrust, then the local cost-free phoenix,
    # and only then disable (aligned with llm-entity-extraction's local-first
    # fallback so tracing never silently turns off).
    if os.environ.get("LANGFUSE_SECRET_KEY"):
        return "langfuse"
    if os.environ.get("BRAINTRUST_API_KEY"):
        return "braintrust"
    if os.environ.get("PHOENIX_TRACING", "enabled").strip().lower() in (
        "1", "true", "enabled", "yes", "on"
    ):
        return "phoenix"
    return "none"


def is_enabled() -> bool:
    return resolve_provider_name() != "none"


def instrument_openai_client(client):
    """Wrap the OpenAI client with the active backend, or return it unchanged."""
    provider = resolve_provider_name()
    try:
        if provider == "langfuse":
            from .langfuse_setup import instrument_openai_client as _langfuse_instrument

            return _langfuse_instrument(client)
        if provider == "braintrust":
            from .braintrust_setup import instrument_openai_client as _braintrust_instrument

            return _braintrust_instrument(client)
        if provider == "phoenix":
            from .phoenix_setup import instrument_openai_client as _phoenix_instrument

            return _phoenix_instrument(client)
    except Exception:
        logger.warning("tracing_instrumentation_failed", provider=provider, exc_info=True)
    return client


@contextmanager
def pipeline_trace(*args, **kwargs):
    """Root chain observation for one document run (one trace per document).

    See `observability/langfuse_setup.pipeline_trace` for parameters. No-ops
    (yields None) unless Langfuse is the active backend. Default ``as_type``
    is ``chain``.
    """
    if resolve_provider_name() != "langfuse":
        yield None
        return
    from .langfuse_setup import pipeline_trace as _langfuse_pipeline_trace

    with _langfuse_pipeline_trace(*args, **kwargs) as root:
        yield root


@contextmanager
def observation(name, **kwargs):
    """Child observation under the active span. No-ops when Langfuse is inactive."""
    if resolve_provider_name() != "langfuse":
        yield None
        return
    from .langfuse_setup import observation as _langfuse_observation

    with _langfuse_observation(name, **kwargs) as span:
        yield span


def _state_summary(state: dict) -> dict:
    state = state or {}
    return {
        "doc_id": state.get("doc_id"),
        "matter_id": state.get("matter_id"),
        "filename": state.get("original_filename"),
        "doc_type": state.get("doc_type"),
        "stage": state.get("stage"),
    }


def _result_summary(result: dict):
    result = result or {}
    out = {
        k: result.get(k)
        for k in (
            "stage",
            "doc_type",
            "classification_confidence",
            "extraction_confidence",
            "review_decision",
            "error_message",
        )
        if k in result
    }
    return out or None


# Data-model observation types (https://langfuse.com/docs/observability/features/observation-types):
# generations = LLM calls (auto via langfuse.openai); agents = specialist
# orchestration; evaluators = quality judges; retrievers = document reads;
# chain = the pipeline as a whole; span = remaining units of work.
NODE_OBSERVATION_TYPES = {
    "document-pipeline": "chain",
    "ingest-document": "span",
    "normalize-intake": "span",
    "extract-image-text": "retriever",
    "transcribe-pdf": "retriever",
    "classify-document": "agent",
    "extract-fields": "agent",
    "judge-verify": "evaluator",
    "arbitrate-verdict": "agent",
    "route-for-review": "span",
    "adjudicate-conflict": "agent",
    "compile-report": "agent",
    "write-catalog": "span",
    "archive-document": "span",
    "pipeline-result": "generation",
    "answer-question": "generation",
}


def observation_type_for(name: str, default: str = "span") -> str:
    return NODE_OBSERVATION_TYPES.get(name, default)


def traced_node(name, *, summarize_input=None, summarize_output=None, as_type=None):
    """Decorator that wraps a graph node fn in a named observation span.

    Applies Langfuse structure best practices: stable, verb-first names
    (`classify-document`, not `classify-<docid>`), typed observations
    (agent/evaluator/retriever rather than a generic span), and curated
    input/output (identifiers + stage/confidence, never raw document text).
    When Langfuse is not the active backend this is a no-op identity decorator.
    """
    if resolve_provider_name() != "langfuse":
        return lambda fn: fn

    from .langfuse_setup import observation

    obs_type = as_type or observation_type_for(name)

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state):
            inp = summarize_input(state) if summarize_input else _state_summary(state)
            with observation(name, as_type=obs_type, input=inp) as span:
                result = fn(state)
                if span is not None:
                    out = summarize_output(result) if summarize_output else _result_summary(result)
                    span.update(output=out)
                return result

        return wrapper

    return deco


def langfuse_call_attrs(name: str, metadata=None) -> dict:
    """Langfuse-specific kwargs to attach to an OpenAI chat call.

    langfuse.openai's `OpenAiArgsExtractor` pulls `name`/`metadata` out of the
    call and uses them for the generation observation — they are never
    forwarded to the OpenAI SDK. Passing `name=<agent_name>` names each
    generation after its agent so traces are easy to read. Returns ``{}`` when
    Langfuse is not the active backend, keeping plain SDK calls valid.
    """
    if resolve_provider_name() != "langfuse":
        return {}
    attrs = {"name": name}
    if metadata is not None:
        attrs["metadata"] = metadata
    return attrs


_flush_ok = 0
_flush_failures = 0


def flush():
    """Flush queued events for the active backend. Safe to call anytime.

    Tracks flush success/failure counters (O-3) so operators can distinguish
    "pipeline healthy, observability dead" from "pipeline idle".
    """
    global _flush_ok, _flush_failures
    provider = resolve_provider_name()
    try:
        if provider == "langfuse":
            from .langfuse_setup import flush_langfuse

            flush_langfuse()
        elif provider == "braintrust":
            from .braintrust_setup import flush_braintrust

            flush_braintrust()
        elif provider == "phoenix":
            from .phoenix_setup import flush_phoenix

            flush_phoenix()
        _flush_ok += 1
    except Exception:
        _flush_failures += 1
        logger.warning("tracing_flush_failed", provider=provider, exc_info=True)


def flush_health() -> dict:
    """Observability health: flush counters + backend state (O-3).

    Surfaces whether events are being delivered or silently dropped, so
    /health and /ops/status can report observability health honestly.
    """
    return {
        "provider": resolve_provider_name(),
        "flush_ok": _flush_ok,
        "flush_failures": _flush_failures,
        "healthy": _flush_failures == 0,
    }


def install_on_dropped() -> None:
    """Wire Langfuse's on_dropped callback (O-3): when the SDK's queue
    overflows or events are dropped, log a warning + bump a counter instead of
    failing silently."""
    try:
        from .langfuse_setup import install_on_dropped as _install

        _install()
    except Exception:
        logger.debug("on_dropped_install_failed")


def get_trace_id():
    """Current trace id for the active backend, or None (disabled/unavailable)."""
    if resolve_provider_name() != "langfuse":
        return None
    from .langfuse_setup import get_trace_id as _langfuse_trace_id

    return _langfuse_trace_id()


def register_atexit_flush():
    """Flush then shut down when the process exits so batched traces land.

    Short-lived jobs (pilots, scripts) must also call ``flush()`` themselves
    before exit; atexit is the last-chance drain for the watcher/API.
    """
    import atexit

    atexit.register(_atexit_flush)


def _atexit_flush():
    flush()
    if resolve_provider_name() != "langfuse":
        return
    try:
        from .langfuse_setup import shutdown_langfuse

        shutdown_langfuse()
    except Exception:
        logger.debug("tracing_shutdown_failed", exc_info=True)


def ensure_process_tracing() -> None:
    """Wire drop-warnings + atexit flush for a short-lived process.

    Call once from script entrypoints after ``load_env()``. Long-running
    services (watcher, API, ops monitor) already do this in ``__main__``.
    """
    install_on_dropped()
    register_atexit_flush()
