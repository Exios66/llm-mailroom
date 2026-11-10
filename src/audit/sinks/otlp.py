"""The ``otlp`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O as span attributes on the
global OpenTelemetry tracer (exported wherever ``OTEL_EXPORTER_OTLP_ENDPOINT`` points). Write-only."""

from __future__ import annotations

import json
from typing import Any


def _tracer():
    from opentelemetry import trace

    return trace.get_tracer("mailroom.audit")


class OtlpSink:
    name = "otlp"

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        with _tracer().start_as_current_span("audit.io") as span:
            span.set_attribute("audit.visit_id", visit_id)
            span.set_attribute("audit.doc_id", str(doc_id or ""))
            span.set_attribute("input.value", prompt or "")
            span.set_attribute("output.value", completion or "")
            return format(span.get_span_context().span_id, "016x")

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        with _tracer().start_as_current_span("audit.pending_row") as span:
            span.set_attribute("audit.record", json.dumps(record, default=str))
            return format(span.get_span_context().span_id, "016x")

    def fetch_pending(self) -> list[dict]:
        return []

    def fetch_io(self, ref: str) -> dict | None:
        return None


__all__ = ["OtlpSink"]
