"""Langfuse tracing backend (langfuse >= 4.x).

Two layers of tracing:

1. **LLM calls** — `get_llm()` in `llm/client.py` passes the OpenAI client
   through `observability.tracing.instrument_openai_client` → here. That
   initializes the Langfuse client and imports `langfuse.openai`, whose
   `register_tracing` monkeypatches OpenAI's `Completions.create`. Every chat
   completion becomes a `generation` observation (model, tokens, cost,
   latency) nested under whatever observation is active in the OTel context.

2. **Pipeline structure** — `pipeline_trace()` opens one root span per document
   run (one trace per document = one self-contained unit of work) with
   `session_id = matter_id`, a deterministic trace id seeded from the file
   name, and curated input/metadata. `observation()` opens child spans for each
   graph node (verb-first, stable names). LLM generations created inside a
   node's `observation()` block automatically nest under it.

Configuration (env):
  LANGFUSE_PUBLIC_KEY   required
  LANGFUSE_SECRET_KEY   required
  LANGFUSE_HOST         base URL (default http://localhost:3000); LANGFUSE_BASE_URL
                        is accepted as an alias for cloud-hosted setups.

Graceful degradation: if keys/host are missing or any init fails, every helper
no-ops and the pipeline runs exactly as if tracing were disabled.
"""

import os
import structlog
from contextlib import contextmanager

logger = structlog.get_logger(__name__)

_langfuse_client = None


class _NoopLangfuse:
    def start_as_current_observation(self, *args, **kwargs):
        return _NoopSpan()

    def start_observation(self, *args, **kwargs):
        return _NoopSpan()

    def update_current_span(self, *args, **kwargs):
        pass

    def set_current_trace_io(self, *args, **kwargs):
        pass

    def create_trace_id(self, *args, **kwargs):
        return None

    def get_current_trace_id(self):
        return None

    def flush(self):
        pass

    def shutdown(self):
        pass

    def trace(self, *args, **kwargs):
        return _NoopSpan()


class _NoopSpan:
    def update(self, *args, **kwargs):
        return self

    def end(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass


def _resolve_host() -> str:
    return os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "http://localhost:3000"


def get_langfuse_client():
    """Return a configured Langfuse client, or a noop stub if unavailable."""
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    if not os.environ.get("LANGFUSE_SECRET_KEY"):
        logger.debug("langfuse_not_configured_no_secret_key")
        _langfuse_client = _NoopLangfuse()
        return _langfuse_client

    try:
        from langfuse import Langfuse

        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local")
        host = _resolve_host()

        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        logger.info("langfuse_initialized", host=host)
    except Exception:
        logger.warning("langfuse_unavailable", exc_info=True)
        _langfuse_client = _NoopLangfuse()

    return _langfuse_client


def instrument_openai_client(client):
    """Return `client`, with langfuse auto-tracing activated.

    langfuse >= 4.x instruments OpenAI by monkeypatching
    `openai.resources.chat.completions.Completions.create` when the
    `langfuse.openai` module is imported (see `register_tracing`). So all we
    need to do is initialize the Langfuse client and import that module — the
    original client (and every OpenAI client in the process) is then traced
    automatically with the exact same interface.
    """
    try:
        get_langfuse_client()
        import langfuse.openai  # noqa: F401  (side effect: registers tracing)
        logger.info("langfuse_openai_tracing_registered")
    except Exception:
        logger.warning("langfuse_client_wrap_failed", exc_info=True)
    return client


@contextmanager
def pipeline_trace(
    *,
    seed=None,
    session_id=None,
    name="document-pipeline",
    input=None,
    metadata=None,
    tags=None,
    environment=None,
):
    """Open the root span of a document's trace.

    One trace per document pipeline execution. Sets a deterministic trace id
    (seeded from `seed`, e.g. the file name) so traces correlate with the
    document in our own system, and propagates session_id/tags/metadata to
    every nested observation (sessions group all documents of a matter).

    Yields the root span (or None when tracing is disabled).
    """
    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        yield None
        return

    from langfuse import propagate_attributes

    trace_context = None
    if seed:
        try:
            trace_context = {"trace_id": client.create_trace_id(seed=str(seed))}
        except Exception:
            logger.warning("langfuse_trace_id_failed", exc_info=True)

    attrs = {
        "session_id": session_id,
        "trace_name": name,
        "metadata": metadata or {},
        "tags": tags or [],
    }
    if environment:
        attrs["environment"] = environment

    with propagate_attributes(**attrs):
        with client.start_as_current_observation(
            as_type="span",
            name=name,
            input=input,
            trace_context=trace_context,
        ) as root:
            yield root


@contextmanager
def observation(name, *, as_type="span", input=None, metadata=None, model=None):
    """Open a child observation under the currently active span/trace.

    Named with active language (`classify-document`, `extract-fields`) per
    Langfuse best practices. LLM generations created inside the `with` block
    automatically nest under it. Yields the observation or None when disabled.
    """
    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        yield None
        return

    kwargs = {"name": name, "as_type": as_type, "input": input}
    if metadata is not None:
        kwargs["metadata"] = metadata
    if model is not None:
        kwargs["model"] = model

    with client.start_as_current_observation(**kwargs) as span:
        yield span


def get_trace_id():
    client = get_langfuse_client()
    try:
        return client.get_current_trace_id()
    except Exception:
        return None


def flush_langfuse():
    client = get_langfuse_client()
    try:
        client.flush()
    except Exception:
        pass


def shutdown_langfuse():
    client = get_langfuse_client()
    try:
        client.shutdown()
    except Exception:
        pass
