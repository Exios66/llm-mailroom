"""Per-stage audit entries for a database the unified audit has not migrated (spec §4.5, rollout).

The unified audit row replaces the old per-stage ``audit_log`` entries (``ingested``,
``classified``, ``extracted``, ``arbiter_decided``, ``routed_to_review``, ``boss_adjudicated``,
``run_aborted``): each is now a note on its node's visit, and the visits land on the one row
a document gets at its terminal node. That row can only be written once the opt-in migration
has run (``audit_meta`` has a ``migration`` record). Until then the terminal row is parked in
the trace sinks, so an unmigrated deployment would have no stage-by-stage compliance trail at
all, which is worse than before the unified audit existed.

``emit_stage_event`` closes that gap: while the migration marker is absent it appends the same
hash-chained per-event entry the pre-unified pipeline wrote; once the database is migrated it
does nothing (the visit notes carry the events and ``audit_log_legacy`` is not extended with
duplicates). Best effort by design: a failure is logged (``audit_gap``) and never fails a run.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: The stage events the pre-unified pipeline wrote, in graph order.
STAGE_EVENTS = (
    "ingested", "classified", "extracted", "arbiter_decided", "routed_to_review", "boss_adjudicated",
    "run_aborted",
)


async def _write(doc_id: str, matter_id: str, event: str, actor: str, detail: dict[str, Any]) -> bool:
    from schemas.audit import build_audit_entry
    from storage.audit_log import (
        AuditLegacyRecord,
        event_log_model,
        get_latest_audit_hash,
        write_audit_entry,
    )
    from storage.db import async_session, ensure_schema

    ensure_schema()
    async with async_session() as session:
        if await event_log_model(session) is AuditLegacyRecord:
            return False  # migrated: the unified row carries this event as a visit note
    entry = build_audit_entry(
        doc_id=doc_id, matter_id=matter_id, event=event, actor=actor, detail=detail,
        prev_hash=await get_latest_audit_hash(doc_id),
    )
    await write_audit_entry(entry)
    return True


def emit_stage_event(state: dict, event: str, actor: str = "pipeline", detail: dict | None = None) -> bool:
    """Append a per-event entry while the audit migration is pending; return whether one was written.

    ``state`` supplies ``doc_id`` and ``matter_id`` and the stage fields recorded with the entry.
    """
    doc_id = state.get("doc_id")
    if not doc_id or event not in STAGE_EVENTS:
        return False
    try:
        from audit.row import _run

        full = {
            **(detail or {}),
            "stage": state.get("stage"),
            "doc_type": state.get("doc_type"),
            "contract_subtype": state.get("contract_subtype"),
            "doc_subclass": state.get("doc_subclass"),
            "classification_confidence": state.get("classification_confidence"),
            "extraction_confidence": state.get("extraction_confidence"),
            "escalation_reason": state.get("escalation_reason"),
            "run_attempt": state.get("run_attempt"),
        }
        return bool(_run(lambda: _write(doc_id, state.get("matter_id") or "DEFAULT", event, actor, full)))
    except Exception:
        logger.warning("audit_gap", event=event, doc_id=doc_id, exc_info=True)
        return False


__all__ = ["STAGE_EVENTS", "emit_stage_event"]
