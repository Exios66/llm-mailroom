"""Trace sinks (spec §5.5).

Until the sink adapters land (roadmap M9), ``record_io`` stores nothing and only
returns the I/O digest, so a visit carries ``io_sha256`` with ``trace_sink: None``.
"""

from __future__ import annotations

from audit.digests import io_sha256


def record_io(*, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> tuple[str | None, str]:
    """Hand one visit's prompt and completion to the sinks.

    Returns ``(first sink that accepted the I/O or None, io_sha256)``.
    """
    return None, io_sha256(prompt, completion)


__all__ = ["record_io"]
