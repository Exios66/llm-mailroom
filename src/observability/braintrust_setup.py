"""Braintrust tracing backend — alternative to Langfuse.

Switch to it with `OBSERVABILITY_PROVIDER=braintrust` and set:

  BRAINTRUST_API_KEY     required (org API key from braintrust.dev)
  BRAINTRUST_PROJECT     project name (default: mailroom)

Wiring: `llm/client.py` → `observability.tracing.instrument_openai_client` →
`instrument_openai_client` here, which calls `braintrust.wrap_openai(client)`.
`wrap_openai` keeps the exact same `.chat.completions.create(...)` interface and
auto-logs every call (prompt, response, tokens, latency) as a Braintrust span
with ``type=llm``, ``name="Chat Completion"``, ``input`` = the messages and
``metadata`` carrying the model.

Structured pipeline tracing: `braintrust_pipeline_trace()` opens one root
``task`` observation per document, and `braintrust_observation()` opens typed
child observations (agent / evaluator / retriever / span / generation) with
verb-first stable names. LLM generations created inside a node's observation
automatically nest under it.

Routing: this module opens a *Logger* for the project (via
``braintrust.init_logger``), NOT an Experiment (``braintrust.init`` is only for
evals/datasets). Logger spans land in the project's **Logs/Traces** view;
Experiment spans land in the Experiments tab and never show up on the trace
page. Every span created here folds ``session_id`` / ``environment`` /
``user_id`` into its ``metadata`` (Braintrust has no top-level columns for
those on Logger spans) and passes ``input`` / ``output`` / ``tags`` /
``metadata`` as top-level event kwargs so the Logs view is fully populated.

Graceful degradation: if the API key is absent or init fails, nothing is
changed and the pipeline runs as if tracing were disabled.
"""

import os
import structlog
from contextlib import contextmanager

logger = structlog.get_logger(__name__)

_configured = False


def is_configured() -> bool:
    return bool(os.environ.get("BRAINTRUST_API_KEY"))


def configure() -> bool:
    """Initialize the Braintrust project Logger (idempotent). True when active.

    Uses ``init_logger`` (project-scoped logging → the Logs/Traces view). Never
    ``init()``: that opens an Experiment, which routes spans to the Experiments
    tab and leaves the project's trace page empty.
    """
    global _configured
    if _configured:
        return True
    if not is_configured():
        logger.debug("braintrust_not_configured_no_api_key")
        return False
    try:
        import braintrust

        braintrust.init_logger(
            project=os.environ.get("BRAINTRUST_PROJECT", "mailroom"),
            api_key=os.environ.get("BRAINTRUST_API_KEY"),
        )
        _configured = True
        logger.info("braintrust_initialized", project=os.environ.get("BRAINTRUST_PROJECT", "mailroom"))
        return True
    except Exception:
        logger.warning("braintrust_initialization_failed", exc_info=True)
        return False


def instrument_openai_client(client):
    """Wrap `client` with Braintrust instrumentation, or return it unchanged."""
    if not configure():
        return client
    try:
        import braintrust

        instrumented = braintrust.wrap_openai(client)
        logger.info("braintrust_openai_client_wrapped")
        return instrumented
    except Exception:
        logger.warning("braintrust_client_wrap_failed", exc_info=True)
        return client


def flush_braintrust():
    if not _configured:
        return
    try:
        import braintrust

        braintrust.flush()
    except Exception:
        pass


# ── Structured pipeline tracing ──────────────────────────────────────────────


class _NoopSpan:
    """Minimal span mock matching the mailroom span surface (name/update/end)."""

    name = ""

    def update(self, *args, **kwargs):
        return self

    def end(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass


_noop_span = _NoopSpan()


class _BTSpan:
    """Adapter bridging the mailroom span surface to a Braintrust ``SpanImpl``.

    The mailroom code calls ``span.update(output=...)`` on the value yielded by
    ``observation()``/``pipeline_trace()`` (e.g. ``agents/intake.py``).
    Braintrust spans log via ``Span.log(...)`` instead, so this wrapper maps the
    langfuse-style ``update`` onto Braintrust ``log`` while exposing ``name``
    and ``end``. Anything else (``id``, ``log_feedback``, ``set_attributes``…)
    is proxied to the underlying span.
    """

    def __init__(self, span):
        self._span = span

    @property
    def name(self):
        try:
            return self._span.name
        except Exception:
            return ""

    def update(self, *args, **kwargs):
        self._span.log(*args, **kwargs)
        return self

    def end(self, *args, **kwargs):
        return self._span.end(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._span, item)

    def __enter__(self):
        self._span.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._span.__exit__(exc_type, exc, tb)


def _noop_pipeline_trace(*args, **kwargs):
    """No-op pipeline trace (yields None)."""
    yield None


def _is_braintrust_available() -> bool:
    """Check if braintrust SDK is importable and minimally functional."""
    try:
        import braintrust  # noqa: F811

        return True
    except Exception:
        return False


def _event_metadata(metadata, *, session_id=None, environment=None, user_id=None, model=None) -> dict:
    """Fold Braintrust-unsupported top-level fields into a copy of metadata."""
    meta = dict(metadata or {})
    if session_id is not None:
        meta.setdefault("session_id", session_id)
    if environment is not None:
        meta.setdefault("environment", environment)
    if user_id is not None:
        meta.setdefault("user_id", user_id)
    if model is not None:
        meta.setdefault("model", str(model))
    return meta


@contextmanager
def braintrust_pipeline_trace(
    *,
    seed=None,
    session_id=None,
    name="document-pipeline",
    input=None,
    metadata=None,
    tags=None,
    environment=None,
    user_id=None,
    as_type="task",
):
    """Open the root observation of a document's trace (Braintrust backend).

    One ``task``-typed span per document pipeline execution. ``seed``
    (e.g. the file name) correlates the trace with the document in our own
    system; ``session_id``/``environment``/``user_id`` ride the span's
    ``metadata``. Yields a span adapter (``update``/``name``/``end``) or the
    no-op mock when tracing is disabled/failed.
    """
    if not _is_braintrust_available():
        yield _noop_span
        return

    # Ensure the project Logger is initialized (project + API key).
    if not configure():
        yield _noop_span
        return

    try:
        import braintrust

        meta = _event_metadata(
            metadata,
            session_id=session_id,
            environment=environment,
            user_id=user_id,
        )
        with braintrust.start_span(
            name=name,
            type=as_type or "task",
            input=input,
            metadata=meta or None,
            tags=list(tags) if tags else None,
        ) as root:
            yield _BTSpan(root)
    except Exception:
        logger.warning("braintrust_pipeline_trace_failed", exc_info=True)
        yield _noop_span


@contextmanager
def braintrust_observation(
    name, *, as_type="span", input=None, metadata=None, model=None
):
    """Open a child observation under the currently active span (Braintrust backend).

    Named with stable, verb-first names (`classify-document`, `extract-fields`).
    ``model`` is folded into ``metadata``. LLM generations created inside the
    ``with`` block (via the wrapped OpenAI client) automatically nest under it.
    Yields a span adapter (``update``/``name``/``end``) or the no-op mock.
    """
    if not _is_braintrust_available():
        yield _noop_span
        return

    # Ensure the project Logger is initialized (project + API key).
    if not configure():
        yield _noop_span
        return

    try:
        import braintrust

        meta = _event_metadata(metadata, model=model)
        with braintrust.start_span(
            name=name,
            type=as_type or "span",
            input=input,
            metadata=meta or None,
        ) as span:
            yield _BTSpan(span)
    except Exception:
        logger.warning("braintrust_observation_failed", exc_info=True)
        yield _noop_span