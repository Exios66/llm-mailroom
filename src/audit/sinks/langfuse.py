"""The ``langfuse`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O as a Langfuse event. Write-only."""

from __future__ import annotations

import json
from typing import Any


def _client():
    """The configured client, or ``None`` when Langfuse is not set up (the no-op stub)."""
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    return None if isinstance(client, _NoopLangfuse) else client


class LangfuseSink:
    name = "langfuse"

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        client = _client()
        if client is None:
            return None
        client.create_event(name="audit-io", input=prompt, output=completion,
                            metadata={"visit_id": visit_id, "doc_id": doc_id})
        return visit_id

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        client = _client()
        if client is None:
            return None
        client.create_event(name="audit-pending-row", input=json.loads(json.dumps(record, default=str)),
                            metadata={"doc_id": record.get("doc_id")})
        return str(record.get("doc_id") or "")

    def fetch_pending(self) -> list[dict]:
        return []

    def fetch_io(self, ref: str) -> dict | None:
        return None


__all__ = ["LangfuseSink"]
