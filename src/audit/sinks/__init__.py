"""Trace sinks (spec §5.5): where a visit's prompt and completion, and pending rows, are kept.

The ``sql`` sink is always on and always first. ``phoenix`` joins automatically
when its packages import, ``PHOENIX_TRACING`` is not disabled, the hosted-container
rule allows it and a 1-second probe of ``PHOENIX_ENDPOINT`` answers. ``langfuse``,
``braintrust`` and ``otlp`` are opt-in through ``MAILROOM_TRACE_SINKS`` (for
example ``sql,phoenix,langfuse``). Any other sink is installed through the
``mailroom.trace_sinks`` entry-point group and is active once installed.

Sinks are additive and independent of the one active tracing provider. A sink
that fails is skipped: it never fails the node. The visit records the first
sink that accepted the I/O as ``trace_sink`` and the others in ``also_in``.
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

import structlog

from audit.digests import io_sha256

logger = structlog.get_logger(__name__)

BUILTIN = ("sql", "phoenix", "langfuse", "braintrust", "otlp")
OPT_IN = ("langfuse", "braintrust", "otlp")
ENTRY_POINT_GROUP = "mailroom.trace_sinks"


@runtime_checkable
class TraceSink(Protocol):
    name: str

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        """Store one visit's I/O; return a reference to it, or ``None`` when not stored."""

    def capture_pending(self, record: dict) -> str | None:
        """Keep a row that could not be committed (§5.6); return a reference."""

    def fetch_pending(self) -> list[dict]:
        """The pending rows this sink holds (empty for write-only sinks)."""

    def fetch_io(self, ref: str) -> dict | None:
        """The I/O stored under ``ref`` (``None`` for write-only sinks)."""


def _requested() -> list[str]:
    raw = os.environ.get("MAILROOM_TRACE_SINKS", "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _entry_points():
    from importlib.metadata import entry_points

    return entry_points(group=ENTRY_POINT_GROUP)


def _builtin(name: str) -> TraceSink | None:
    if name == "sql":
        from .sql import SqlSink

        return SqlSink()
    if name == "phoenix":
        from .phoenix import PhoenixSink

        return PhoenixSink() if PhoenixSink.usable() else None
    if name == "langfuse":
        from .langfuse import LangfuseSink

        return LangfuseSink()
    if name == "braintrust":
        from .braintrust import BraintrustSink

        return BraintrustSink()
    if name == "otlp":
        from .otlp import OtlpSink

        return OtlpSink()
    return None


def active_sinks() -> list[TraceSink]:
    """The sinks in use now, ``sql`` first. Read from the environment on every call."""
    requested = _requested()
    sinks: list[TraceSink] = []
    for name in ("sql", "phoenix", *[n for n in OPT_IN if n in requested]):
        try:
            sink = _builtin(name)
        except Exception:
            logger.warning("trace_sink_unavailable", sink=name, exc_info=True)
            sink = None
        if sink is not None:
            sinks.append(sink)
    try:
        for ep in _entry_points():
            if ep.name in BUILTIN:
                continue  # the built-ins follow the rules above
            try:
                obj = ep.load()
                # A class or factory is called; an instance is used as is.
                factory = isinstance(obj, type) or (callable(obj) and not isinstance(obj, TraceSink))
                sink = obj() if factory else obj
                if isinstance(sink, TraceSink):
                    sinks.append(sink)
                else:
                    logger.warning("trace_sink_invalid", sink=ep.name)
            except Exception:
                logger.warning("trace_sink_load_failed", sink=ep.name, exc_info=True)
    except Exception:
        logger.warning("trace_sink_entry_points_failed", exc_info=True)
    return sinks


def record_io(
    *, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None
) -> tuple[str | None, str, list[str]]:
    """Hand one visit's prompt and completion to every active sink.

    Returns ``(first sink that accepted it or None, io_sha256, the other sinks that accepted it)``.
    """
    accepted: list[str] = []
    for sink in active_sinks():
        try:
            if sink.record_io(visit_id, doc_id, prompt, completion) is not None:
                accepted.append(sink.name)
        except Exception:
            logger.warning("trace_sink_record_failed", sink=getattr(sink, "name", "?"), visit_id=visit_id,
                           exc_info=True)
    return (accepted[0] if accepted else None), io_sha256(prompt, completion), accepted[1:]


def _size(record: dict[str, Any]) -> int:
    import json

    return len(json.dumps(record, default=str).encode())


def capture_pending(record: dict[str, Any], *, partial=None) -> list[dict[str, Any]]:
    """Hand a row that could not be committed to every sink (§5.6 step 1).

    A sink with ``max_payload_bytes`` smaller than the record gets the
    decisions-only copy that ``partial()`` builds (marked ``complete: false``).
    Returns ``[{"sink", "ref", "complete"}]`` for each sink that kept a copy.
    """
    kept: list[dict[str, Any]] = []
    small: dict[str, Any] | None = None
    full_size = _size(record)
    for sink in active_sinks():
        name = getattr(sink, "name", "?")
        payload = record
        limit = getattr(sink, "max_payload_bytes", None)
        if limit is not None and full_size > limit:
            if partial is None:
                logger.warning("trace_sink_payload_too_large", sink=name, size=full_size, limit=limit)
                continue
            small = small if small is not None else partial()
            payload = small
            if _size(payload) > limit:
                logger.warning("trace_sink_payload_too_large", sink=name, size=_size(payload), limit=limit)
                continue
        try:
            ref = sink.capture_pending(payload)
        except Exception:
            logger.warning("trace_sink_capture_failed", sink=name, exc_info=True)
            continue
        if ref is not None:
            kept.append({"sink": name, "ref": ref, "complete": bool(payload.get("complete", True))})
    return kept


__all__ = ["BUILTIN", "ENTRY_POINT_GROUP", "OPT_IN", "TraceSink", "active_sinks", "capture_pending", "record_io"]
