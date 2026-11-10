"""Events on a document outside its run: an addendum after the row, a trace visit before it (spec §4.3).

A ``record`` / ``requeue`` review decision or a relations link can happen while
the document is parked (no row yet) or after it finished (it has a row).
``record_document_event`` writes an addendum in the second case. In the first
it appends a visit to the parked checkpoint's ``audit_trace`` through
``graph.update_state``, so the reducer applies and the visit lands on the row
when the run ends (review item A2). Best-effort: it never raises.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

#: Addendum kind -> the node name of the visit recorded before the row exists.
EVENT_NODES = {"review_decision": "human_review", "relations_linked": "relations"}


def _append_to_checkpoint(doc_id: str, kind: str, actor: str, detail: dict[str, Any]) -> bool:
    """Append one visit to the document's parked checkpoint; False when there is none."""
    from audit.trace import visit
    from graph.build_graph import get_compiled_graph
    from pipeline.bins import load_manifest

    manifest = load_manifest(doc_id)
    thread = getattr(manifest, "checkpoint_thread_id", None) if manifest else None
    if not thread:
        return False
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": thread}}
    snapshot = graph.get_state(config)
    if snapshot is None or not getattr(snapshot, "next", None):
        return False  # nothing parked on this thread: the run already ended
    now = datetime.now(timezone.utc)
    record = visit(
        EVENT_NODES.get(kind, kind), {"doc_id": doc_id}, {}, started_at=now, ended_at=now,
        event={"event": kind, "actor": actor, "detail": dict(detail)},
    )
    graph.update_state(config, {"audit_trace": [record]})
    return True


async def record_document_event(
    doc_id: str, kind: str, detail: dict[str, Any], *, actor: str = "human_reviewer"
) -> Literal["addendum", "trace"] | None:
    """An addendum when the document has a row, else a visit on its parked checkpoint.

    Returns which one was written, or ``None`` when neither could be (no row and
    no parked checkpoint, the audit tables not migrated, or a write error; each
    is logged as ``audit_event_unrecorded``).
    """
    from audit.models import Addendum
    from audit.writer import get_row, write_addendum

    try:
        if await get_row(doc_id) is not None:
            await write_addendum(Addendum(doc_id=doc_id, kind=kind, actor=actor, detail=dict(detail)))
            return "addendum"
        if await asyncio.to_thread(_append_to_checkpoint, doc_id, kind, actor, detail):
            return "trace"
        logger.warning("audit_event_unrecorded", doc_id=doc_id, kind=kind, reason="no_row_no_checkpoint")
    except Exception:
        logger.warning("audit_event_unrecorded", doc_id=doc_id, kind=kind, exc_info=True)
    return None


__all__ = ["EVENT_NODES", "record_document_event"]
