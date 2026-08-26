"""Langfuse tracing backend (langfuse >= 4.x).

Two layers of tracing:

1. **LLM calls** — `get_llm()` in `llm/client.py` passes the OpenAI client
   through `observability.tracing.instrument_openai_client` → here. That
   initializes the Langfuse client and imports `langfuse.openai`, whose
   `register_tracing` monkeypatches OpenAI's `Completions.create`. Every chat
   completion becomes a `generation` observation (model, tokens, cost,
   latency) nested under whatever observation is active in the OTel context.

2. **Pipeline structure** — `pipeline_trace()` opens one root *chain*
   observation per document (one trace = one unit of work) with
   `session_id = matter_id`, a deterministic trace id seeded from the file
   name, and curated input/metadata. `observation()` opens typed children
   (agent / evaluator / retriever / span / generation) with verb-first
   stable names. LLM generations created inside a node's observation
   automatically nest under it.

Configuration (env):
  LANGFUSE_PUBLIC_KEY   required
  LANGFUSE_SECRET_KEY   required
  LANGFUSE_HOST         base URL (default http://localhost:3000); LANGFUSE_BASE_URL
                        is accepted as an alias for cloud-hosted setups.
  LANGFUSE_FLUSH_AT / LANGFUSE_FLUSH_INTERVAL
                        SDK batching (event count / seconds). Defaults 512 / 5s.
                        Short-lived jobs still MUST call flush() before exit —
                        the background exporter may not drain otherwise.
  LANGFUSE_RELEASE      optional release/version label on every observation.
  OBSERVABILITY_ENVIRONMENT / LANGFUSE_TRACING_ENVIRONMENT
                        client-default environment (live/pilot/misc/mock).

Data model (https://langfuse.com/docs/observability/data-model):
  observations nested in one trace (trace_id), traces grouped by session_id,
  attributes (environment, tags, metadata, optional user_id, release)
  copied onto every observation. Tracing is non-blocking: events enqueue
  locally and a background exporter sends batches.

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


def _optional_int(name: str):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("langfuse_invalid_int_env", name=name, value=raw)
        return None


def _optional_float(name: str):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("langfuse_invalid_float_env", name=name, value=raw)
        return None


def _release_label() -> str:
    explicit = os.environ.get("LANGFUSE_RELEASE")
    if explicit:
        return explicit
    try:
        from importlib.metadata import version

        return f"mailroom@{version('mailroom')}"
    except Exception:
        return "mailroom"


def client_kwargs() -> dict:
    """Constructor kwargs for the Langfuse SDK (batching + data-model attrs).

    ``flush_at`` / ``flush_interval`` are the Python SDK's batch size and
    timer (https://langfuse.com/docs/observability/features/queuing-batching).
    Environment and release land on every observation so dashboards can
    separate live/pilot/misc without per-span plumbing.
    """
    kwargs: dict = {
        "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local"),
        "secret_key": os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local"),
        "host": _resolve_host(),
        "release": _release_label(),
    }
    environment = (
        os.environ.get("OBSERVABILITY_ENVIRONMENT")
        or os.environ.get("LANGFUSE_TRACING_ENVIRONMENT")
    )
    if environment:
        kwargs["environment"] = environment
    flush_at = _optional_int("LANGFUSE_FLUSH_AT")
    if flush_at is not None:
        kwargs["flush_at"] = flush_at
    flush_interval = _optional_float("LANGFUSE_FLUSH_INTERVAL")
    if flush_interval is not None:
        kwargs["flush_interval"] = flush_interval
    timeout = _optional_int("LANGFUSE_TIMEOUT")
    if timeout is not None:
        kwargs["timeout"] = timeout
    sample_rate = _optional_float("LANGFUSE_SAMPLE_RATE")
    if sample_rate is not None:
        kwargs["sample_rate"] = sample_rate
    return kwargs


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

        kwargs = client_kwargs()
        _langfuse_client = Langfuse(**kwargs)
        logger.info(
            "langfuse_initialized",
            host=kwargs.get("host"),
            environment=kwargs.get("environment"),
            flush_at=kwargs.get("flush_at"),
            flush_interval=kwargs.get("flush_interval"),
            release=kwargs.get("release"),
        )
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
    user_id=None,
    as_type="chain",
):
    """Open the root chain observation of a document's trace.

    One trace per document pipeline execution (data-model: one request /
    unit of work). Sets a deterministic trace id (seeded from `seed`, e.g.
    the file name) so traces correlate with the document in our own system,
    and propagates session_id/tags/metadata/environment/user_id to every
    nested observation. Root type is ``chain`` — the pipeline is a sequence
    of specialist steps, not a single LLM generation.

    Yields the root observation (or None when tracing is disabled).
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
    if user_id:
        attrs["user_id"] = user_id

    with propagate_attributes(**attrs):
        with client.start_as_current_observation(
            as_type=as_type,
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


def install_on_dropped() -> None:
    """Warn when the SDK drops events (O-3).

    Langfuse Python v4 has no ``on_dropped`` callback (that was a v2/v3
    queue hook). Overflows are logged by the SDK itself; we still attach
    a callback when the attribute exists so older SDKs keep working, and
    we always wire flush_health counters via ``tracing.flush``.
    """
    try:
        client = get_langfuse_client()
        if isinstance(client, _NoopLangfuse):
            return
        if hasattr(client, "on_dropped"):
            client.on_dropped = lambda dropped: logger.warning(
                "langfuse_events_dropped", dropped=len(dropped or [])
            )
    except Exception:
        logger.debug("on_dropped_wire_failed")


def shutdown_langfuse():
    client = get_langfuse_client()
    try:
        client.shutdown()
    except Exception:
        pass
