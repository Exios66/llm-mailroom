"""Arize Phoenix tracing backend — local, cost-free default for llm-mailroom.

Mirrors the architecture of ``llm-entity-extraction/src/phoenix_tracing.py``
(commit-aligned so both repos share the same local-first observability
story): Phoenix is Apache/Elastic-licensed, runs as a single local process
with SQLite/in-memory storage, and is OpenTelemetry-native. The DB can be
deleted when a batch is done — ideal for pour-in, poke-around, discard
workflows, and it costs nothing on top of the LLM API calls.

Design / alignment
------------------
- One OpenTelemetry ``TracerProvider`` is lazily initialised per process.
  The provider emits spans over OTLP HTTP to ``PHOENIX_ENDPOINT`` (default
  ``http://localhost:6006/v1/traces``).
- ``instrument_openai_client`` uses the OpenInference ``OpenAIInstrumentor``
  (part of the arize-phoenix dependency set) so every LLM call lands as a
  nested span with model, token usage, latency and response — identical to
  what the Langfuse backend captures, but stored locally for free.
- The node-level structured spans (``pipeline_trace`` / ``observation``)
  are Langfuse-only in the facade. When only Phoenix is active, LLM
  generations still trace (via the OpenAI instrumentor) and ``flush()``
  force-exports them; the Langfuse-only helpers no-op, so the pipeline runs
  identically.
- Graceful degradation: the module is a no-op when ``PHOENIX_TRACING`` is
  disabled or provider initialisation failed — runs continue exactly as if
  observability were off. ``flush()`` force-flushes the batch processor
  without shutting the provider down.

Configuration (env):
  PHOENIX_TRACING       enable/disable (default: enabled)
  PHOENIX_ENDPOINT      OTLP HTTP endpoint (default http://localhost:6006/v1/traces)
  PHOENIX_SERVICE_NAME  OTel service name (default mailroom)
  PHOENIX_PROJECT       openinference project name (default mailroom)
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "enabled", "yes", "on"}

_provider = None
_instrumented = False


def phoenix_enabled() -> bool:
    """Return True when Phoenix tracing should be active (default: enabled)."""
    return os.environ.get("PHOENIX_TRACING", "enabled").strip().lower() in _TRUE_VALUES


def _instrument_openai() -> None:
    """Best-effort OpenInference instrumentation of the OpenAI SDK."""
    global _instrumented
    if _instrumented:
        return
    try:
        from openinference.instrumentation import TraceConfig
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument(config=TraceConfig(hide_input_text=True))
        _instrumented = True
        logger.info("phoenix_openai_instrumented")
    except Exception:  # noqa: BLE001 - observability must never break the run
        logger.warning("phoenix_openai_instrument_failed", exc_info=True)


def _init_opentelemetry():
    """Initialise OpenTelemetry SDK with Phoenix OTLP exporter, once."""
    global _provider
    if _provider is not None:
        return _provider
    if not phoenix_enabled():
        logger.info("phoenix_tracing_disabled")
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006/v1/traces")
        service_name = os.environ.get("PHOENIX_SERVICE_NAME", "mailroom")
        project = os.environ.get("PHOENIX_PROJECT", "mailroom")
        resource = Resource.create(
            {
                "service.name": service_name,
                "openinference.project.name": project,
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _provider = provider
        logger.info(
            "phoenix_tracing_initialized",
            extra={"endpoint": endpoint, "service": service_name, "project": project},
        )
        _instrument_openai()
        return provider
    except Exception:  # noqa: BLE001
        logger.warning("phoenix_tracing_init_failed", exc_info=True)
        return None


def instrument_openai_client(client):
    """Return ``client`` with Phoenix auto-tracing activated (no-op if disabled).

    Initialising the OTel provider + OpenAI instrumentor patches the OpenAI SDK so
    every chat completion becomes a traced generation span. Same interface as the
    Langfuse/Braintrust backends — the facade calls this when Phoenix is active.
    """
    _init_opentelemetry()
    return client


def flush_phoenix() -> None:
    """Force-export buffered spans without disabling the provider."""
    if _provider is None:
        return
    try:
        _provider.force_flush()
    except Exception:  # noqa: BLE001
        logger.warning("phoenix_flush_failed", exc_info=True)


def is_configured() -> bool:
    return phoenix_enabled()