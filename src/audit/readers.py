"""Readers over the unified audit row (spec §7): ``GET /audit/{doc_id}`` and ``GET /audit``.

``doc_view`` is one document's row, sub-rows and addenda as a flat ``entries``
list, with the legacy per-event chain, any pending record and its warehouse
export beside it. ``unified_summary`` is what ``GET /audit`` and
``analyze_audit_db.py`` add to the legacy summary: rows by event and outcome,
addenda by kind, attempts per stage, retry and rescue rates, pending and
recovered counts, and the results of the verifier's checks.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

PENDING_STATES = ("pending", "pending_partial")


def _entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Row, its sub-rows, then each addendum followed by its sub-rows; each with ``kind`` and ``parent_id``."""
    head = {k: v for k, v in row.items() if k not in ("sub_rows", "addenda")}
    out = [{**head, "kind": "row", "parent_id": None}]
    out += [{**s, "kind": "attempt"} for s in row.get("sub_rows") or []]
    for add in row.get("addenda") or []:
        out.append({**{k: v for k, v in add.items() if k not in ("sub_rows", "kind")},
                    "kind": "addendum", "addendum_kind": add.get("kind"), "parent_id": row["entry_id"]})
        out += [{**s, "kind": "attempt"} for s in add.get("sub_rows") or []]
    return out


async def _legacy(doc_id: str) -> dict[str, Any]:
    from schemas.audit import AuditLogEntry, verify_legacy_chain
    from storage.audit_log import get_audit_chain

    records = await get_audit_chain(doc_id)
    entries = [
        AuditLogEntry(entry_id=r["entry_id"], doc_id=doc_id, matter_id=r.get("matter_id") or "", event=r["event"],
                      actor=r["actor"], detail=r["detail"], prev_hash=r["prev_hash"],
                      entry_hash=r["entry_hash"], timestamp=r["timestamp"])
        for r in records
    ]
    return {
        "chain_length": len(records),
        "chain_valid": verify_legacy_chain(entries) if entries else True,
        "entries": [{**r, "timestamp": r["timestamp"].isoformat() if hasattr(r["timestamp"], "isoformat")
                     else r["timestamp"]} for r in records],
    }


def _pending(doc_id: str) -> dict[str, Any] | None:
    try:
        from pipeline.bins import load_manifest

        manifest = load_manifest(doc_id)
    except Exception:
        return None
    if manifest is None or getattr(manifest, "audit_written", None) not in PENDING_STATES:
        return None
    return {"audit_written": manifest.audit_written, "refs": list(manifest.audit_pending_refs or [])}


def _warehouse(doc_id: str) -> dict[str, Any] | None:
    try:
        from storage.warehouse import load_warehouse_manifest

        entry = (load_warehouse_manifest().get("documents") or {}).get(doc_id)
    except Exception:
        return None
    if not isinstance(entry, dict):
        return None
    return {"export_date": entry.get("export_date"), "files": list(entry.get("files") or [])}


async def doc_valid(doc_id: str) -> bool:
    """The document's row, addenda chain and sub-rows recompute to their stored hashes."""
    from sqlalchemy import select

    from audit import verify
    from storage.audit_log import AuditAddendumRecord, AuditAttemptRecord, AuditLogRecord
    from storage.db import async_session

    async with async_session() as session:
        rows = (await session.execute(select(AuditLogRecord).where(AuditLogRecord.doc_id == doc_id))).scalars().all()
        adds = (await session.execute(
            select(AuditAddendumRecord).where(AuditAddendumRecord.doc_id == doc_id).order_by(AuditAddendumRecord.seq)
        )).scalars().all()
        subs = (await session.execute(
            select(AuditAttemptRecord).where(AuditAttemptRecord.doc_id == doc_id))).scalars().all()
    results = [verify.CheckResult() for _ in range(3)]
    for row in rows:
        expected = verify.hash_entry(
            prev_hash=row.prev_hash, doc_id=row.doc_id, entry_id=row.entry_id, event=row.event,
            detail=row.detail or {}, matter_id=row.matter_id, actor=row.actor, timestamp=row.timestamp,
        )
        if expected != row.entry_hash:
            results[0].fail("entry_hash_mismatch", doc_id=doc_id)
    verify._check_addenda(rows, adds, results[1])
    verify._check_subrows(rows, adds, subs, results[2])
    return all(r.ok for r in results)


async def doc_view(doc_id: str) -> dict[str, Any]:
    """``GET /audit/{doc_id}`` (spec §7). A document with no row has empty ``entries``."""
    from audit.models import AuditMigrationPending
    from audit.writer import get_row

    try:
        row = await get_row(doc_id)
    except AuditMigrationPending:
        row = None  # before the migration every entry is still legacy
    entries = _entries(row) if row else []
    return {
        "doc_id": doc_id,
        "chain_length": 1 + len(row.get("addenda") or []) if row else 0,
        "chain_valid": await doc_valid(doc_id) if row else True,
        "entries": entries,
        "legacy": await _legacy(doc_id),
        "pending": _pending(doc_id),
        "warehouse": _warehouse(doc_id),
    }


async def all_rows() -> list[dict[str, Any]]:
    """Every document's ``get_row`` result, in chain order (empty before the migration)."""
    from sqlalchemy import select

    from audit.models import AuditMigrationPending
    from audit.writer import get_row
    from storage.audit_log import AuditLogRecord
    from storage.db import async_session, ensure_schema

    ensure_schema()
    async with async_session() as session:
        doc_ids = [d for (d,) in (await session.execute(
            select(AuditLogRecord.doc_id).order_by(AuditLogRecord.seq))).all()]
    out = []
    for doc_id in doc_ids:
        try:
            row = await get_row(doc_id)
        except AuditMigrationPending:
            return []
        if row is not None:
            out.append(row)
    return out


def _rate(n: int, total: int) -> float | None:
    return round(n / total, 4) if total else None


def _pending_count() -> int:
    from pipeline.bins import manifests_dir

    mdir = manifests_dir()
    if not mdir.is_dir():
        return 0
    n = 0
    for path in mdir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("audit_written") in PENDING_STATES:
            n += 1
    return n


async def unified_summary(*, verify: bool = True) -> dict[str, Any]:
    """The unified-row part of ``GET /audit`` (spec §7)."""
    from audit.metrics import metrics_for

    rows = await all_rows()
    by_event, by_outcome, add_kinds, per_stage = Counter(), Counter(), Counter(), Counter()
    retried = rescued = recovered = 0
    for row in rows:
        m = metrics_for(row)
        by_event[row["event"]] += 1
        by_outcome[str(m["outcome"])] += 1
        add_kinds.update(a["kind"] for a in row.get("addenda") or [])
        subs = list(row.get("sub_rows") or []) + [s for a in row.get("addenda") or [] for s in a.get("sub_rows") or []]
        per_stage.update(s["stage"] for s in subs)
        retried += any(s.get("trigger") == "retry" for s in subs)
        rescued += bool(m["arbiter_rescued"])
        recovered += bool(m["recovered"])
    out: dict[str, Any] = {
        "rows": len(rows),
        "rows_by_event": dict(by_event),
        "rows_by_outcome": dict(by_outcome),
        "addenda_by_kind": dict(add_kinds),
        "attempts_by_stage": dict(per_stage),
        "retry_rate": _rate(retried, len(rows)),
        "rescue_rate": _rate(rescued, len(rows)),
        "pending": _pending_count(),
        "recovered": recovered,
        "checks": None,
    }
    if verify:
        from audit.verify import SECTIONS, verify_audit_db

        report = await verify_audit_db()
        out["checks"] = {"ok": report.ok, **{
            name: {"ok": getattr(report, name).ok, "checked": getattr(report, name).checked,
                   "failures": len(getattr(report, name).failures)} for name in SECTIONS}}
    return out


__all__ = ["all_rows", "doc_valid", "doc_view", "unified_summary"]
