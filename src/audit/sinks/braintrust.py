"""The ``braintrust`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O logged on the current span. Write-only."""

from __future__ import annotations

import json
from typing import Any


def _span():
    import braintrust

    return braintrust.current_span()


class BraintrustSink:
    name = "braintrust"

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        span = _span()
        span.log(input=prompt, output=completion, metadata={"audit_visit_id": visit_id, "doc_id": doc_id})
        return visit_id

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        _span().log(metadata={"audit_pending_row": json.dumps(record, default=str)})
        return str(record.get("doc_id") or "")

    def fetch_pending(self) -> list[dict]:
        return []

    def fetch_io(self, ref: str) -> dict | None:
        return None


__all__ = ["BraintrustSink"]
