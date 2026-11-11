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
    """The document's row, addenda chain and sub-rows recompute to their stored hashes.

    A per-document scope of the same rules the full verifier applies: the row
    hash, each addendum's hash and its link to the row/per-document chain, every
    sub-row's hash under its own parent's matter id and actor, and the parent's
    ``detail.attempts`` membership (each sub-row listed exactly once with its
    stored hash). Database read errors propagate.
    """
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

    row_report, add_report, sub_report = (
        verify.CheckReport(), verify.CheckReport(), verify.CheckReport()
    )

    for row in rows:
        try:
            if verify._row_hash(row) != row.entry_hash:
                row_report.fail(kind="entry_hash_mismatch", doc_id=doc_id)
        except Exception as exc:  # invalid stored timestamp/type
            verify._hash_failure(row_report, exc, doc_id=doc_id, entry_id=row.entry_id)

    row = rows[0] if len(rows) == 1 else None
    matter_id = row.matter_id if row is not None else (rows[0].matter_id if rows else "")
    prev_hash = row.entry_hash if row is not None else ""
    for add in adds:
        add_report.checked += 1
        if add.prev_hash != prev_hash:
            add_report.fail(kind="prev_hash_mismatch", addendum_id=add.addendum_id)
        try:
            if verify._addendum_hash(add, matter_id=matter_id) != add.entry_hash:
                add_report.fail(kind="entry_hash_mismatch", addendum_id=add.addendum_id)
        except Exception as exc:
            verify._hash_failure(add_report, exc, addendum_id=add.addendum_id)
        prev_hash = add.entry_hash

    # parent_id -> (matter_id, actor, detail) for the row and every addendum.
    parents: dict[str, tuple[str, str, Any]] = {}
    if row is not None:
        parents[row.entry_id] = (row.matter_id, row.actor, row.detail)
    for add in adds:
        parents[add.addendum_id] = (matter_id, add.actor, add.detail)

    sub_report.checked = len(subs)
    for sub in subs:
        parent = parents.get(sub.parent_id)
        if parent is None:
            sub_report.fail(kind="parent_missing", attempt_id=sub.attempt_id, parent_id=sub.parent_id)
            continue
        sub_matter, sub_actor, _detail = parent
        if not isinstance(sub.detail, dict):
            sub_report.fail(kind="detail_not_object", attempt_id=sub.attempt_id)
        else:
            try:
                expected = verify.attempt_hash(
                    verify._attempt_sub(sub), matter_id=sub_matter, actor=sub_actor
                )
            except Exception as exc:
                verify._hash_failure(sub_report, exc, attempt_id=sub.attempt_id)
            else:
                if sub.entry_hash != expected:
                    sub_report.fail(kind="hash_mismatch", attempt_id=sub.attempt_id)
        if sub.doc_id != doc_id:
            sub_report.fail(
                kind="doc_mismatch", attempt_id=sub.attempt_id, expected=doc_id, actual=sub.doc_id
            )
        refs = (parents[sub.parent_id][2] or {}).get("attempts") or []
        listed = {
            ref.get("attempt_id"): ref.get("entry_hash")
            for ref in refs
            if isinstance(ref, dict)
        }
        if listed.get(sub.attempt_id) != sub.entry_hash:
            sub_report.fail(
                kind="not_listed_by_parent", attempt_id=sub.attempt_id, parent_id=sub.parent_id
            )

    return row_report.ok and add_report.ok and sub_report.ok


async def doc_view(doc_id: str) -> dict[str, Any]:
    """Build the ``GET /audit/{doc_id}`` view (spec §7).

    With no unified row, ``entries`` is empty and ``chain_valid`` is ``True``;
    the legacy chain and any pending or warehouse references are still read.
    Only a pending audit migration is treated as an absent unified row.
    """
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
    """Summarize unified rows for ``GET /audit`` (spec §7).

    Rates use the number of rows as their denominator and are ``None`` when
    there are no rows. ``verify=False`` leaves ``checks`` as ``None``; otherwise
    the full audit verifier supplies the section results.
    """
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
