"""The ``braintrust`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O logged on the current span. Write-only."""

from __future__ import annotations

import json
from typing import Any


def _span():
    """The active span, or ``None`` when there is none (the SDK's no-op span records nothing)."""
    import braintrust

    span = braintrust.current_span()
    return None if span is braintrust.NOOP_SPAN else span


class BraintrustSink:
    name = "braintrust"
    recoverable = False  # write-only: ``fetch_pending`` is always empty
    off_box = True  # sends data out of the process: pending rows only with audit.full_trace

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        span = _span()
        if span is None:
            return None  # nothing was recorded: report no reference
        span.log(input=prompt, output=completion, metadata={"audit_visit_id": visit_id, "doc_id": doc_id})
        return visit_id

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        span = _span()
        if span is None:
            return None
        span.log(metadata={"audit_pending_row": json.dumps(record, default=str)})
        return str(record.get("doc_id") or "")

    def fetch_pending(self) -> list[dict]:
        return []

    def fetch_io(self, ref: str) -> dict | None:
        return None


__all__ = ["BraintrustSink"]
