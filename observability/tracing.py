"""Observability facade — picks the active tracing backend.

Backend selection (env `OBSERVABILITY_PROVIDER`):

  auto        (default) langfuse if LANGFUSE_SECRET_KEY is set, else braintrust
              if BRAINTRUST_API_KEY is set, else none
  langfuse    force Langfuse
  braintrust  force Braintrust
  none        disable tracing entirely

Two integration points:

- `instrument_openai_client` wraps the OpenAI client built in
  `llm/client.py:get_llm`, so every LLM call becomes a traced generation.
- `pipeline_trace` / `traced_node` add structured, nested observations around
  document runs and graph nodes (currently Langfuse-only; both backends keep
  working for LLM calls). All helpers no-op safely when tracing is disabled.
"""

import functools
import os
import structlog
from contextlib import contextmanager

logger = structlog.get_logger(__name__)


def resolve_provider_name() -> str:
    """Return one of: 'langfuse', 'braintrust', 'none'."""
    choice = os.environ.get("OBSERVABILITY_PROVIDER", "auto").strip().lower()

    if choice in ("langfuse", "braintrust", "none"):
        return choice

    # auto: prefer langfuse, then braintrust, then disable
    if os.environ.get("LANGFUSE_SECRET_KEY"):
        return "langfuse"
    if os.environ.get("BRAINTRUST_API_KEY"):
        return "braintrust"
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
    except Exception:
        logger.warning("tracing_instrumentation_failed", provider=provider, exc_info=True)
    return client


@contextmanager
def pipeline_trace(*args, **kwargs):
    """Root span for one document run (one trace per document).

    See `observability/langfuse_setup.pipeline_trace` for parameters. No-ops
    (yields None) unless Langfuse is the active backend.
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


def traced_node(name, *, summarize_input=None, summarize_output=None):
    """Decorator that wraps a graph node fn in a named observation span.

    Applies Langfuse structure best practices: stable, verb-first names
    (`classify-document`, not `classify-<docid>`), and curated input/output
    (identifiers + stage/confidence, never raw document text). When Langfuse is
    not the active backend this is a no-op identity decorator.
    """
    if resolve_provider_name() != "langfuse":
        return lambda fn: fn

    from .langfuse_setup import observation

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(state):
            inp = summarize_input(state) if summarize_input else _state_summary(state)
            with observation(name, input=inp) as span:
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


def flush():
    """Flush queued events for the active backend. Safe to call anytime."""
    provider = resolve_provider_name()
    try:
        if provider == "langfuse":
            from .langfuse_setup import flush_langfuse

            flush_langfuse()
        elif provider == "braintrust":
            from .braintrust_setup import flush_braintrust

            flush_braintrust()
    except Exception:
        pass


def register_atexit_flush():
    """Flush pending traces when the process exits (so the last events land)."""
    import atexit

    atexit.register(flush)
