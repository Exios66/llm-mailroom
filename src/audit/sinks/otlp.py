"""The ``otlp`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O as span attributes on the
global OpenTelemetry tracer. Write-only.

Spans are exported only if a ``TracerProvider`` is installed in the process (the Phoenix
setup in ``observability.phoenix_setup`` installs the shared one). Setting
``OTEL_EXPORTER_OTLP_ENDPOINT`` alone configures nothing: without a provider the tracer is a
no-op, its spans do not record, and this sink returns ``None`` instead of a reference.
Prompt and completion text reaches it only with ``audit.full_trace`` on.
"""

from __future__ import annotations

import json
from typing import Any


def _tracer():
    from opentelemetry import trace

    return trace.get_tracer("mailroom.audit")


class OtlpSink:
    name = "otlp"
    recoverable = False  # write-only: ``fetch_pending`` is always empty
    off_box = True  # sends data out of the process: pending rows only with audit.full_trace

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        with _tracer().start_as_current_span("audit.io") as span:
            span.set_attribute("audit.visit_id", visit_id)
            span.set_attribute("audit.doc_id", str(doc_id or ""))
            span.set_attribute("input.value", prompt or "")
            span.set_attribute("output.value", completion or "")
            if not span.is_recording():
                return None  # a no-op span stored nothing; even a valid propagated context proves nothing
            return format(span.get_span_context().span_id, "016x")

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        with _tracer().start_as_current_span("audit.pending_row") as span:
            span.set_attribute("audit.record", json.dumps(record, default=str))
            if not span.is_recording():
                return None
            return format(span.get_span_context().span_id, "016x")

    def fetch_pending(self) -> list[dict]:
        return []

    def fetch_io(self, ref: str) -> dict | None:
        return None


__all__ = ["OtlpSink"]
