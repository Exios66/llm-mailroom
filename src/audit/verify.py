"""Verify the unified audit DB (spec §6, roadmap M4).

``verify_audit_db`` runs five checks plus the pending report:

1. **main** - ``audit_log`` rows in ``seq`` order: ``prev_hash`` links, recomputed
   hashes, unique ``doc_id``; a recovered row's ``recovered_from.detail_sha256``
   must match its ``detail`` without ``recovered_from``.
2. **subrows** - every ``audit_attempts`` row belongs to a row or addendum that
   lists it in ``detail.attempts`` with the same hash, and its hash recomputes.
   Missing, extra and edited sub-rows fail.
3. **addenda** - per document, the first addendum links to the row's hash and
   each later one to the previous addendum; hashes recompute; ``seq`` runs 1..n.
4. **legacy** - today's per-document check on the per-event table
   (``audit_log_legacy`` after the migration, ``audit_log`` before it).
5. **trace_io** - every visit ``io_sha256`` in a row, addendum or sub-row
   against the ``sql`` sink (``audit_trace_io``), and every stored I/O record
   against its own ``sha256``. External sinks are checked only with
   ``check_sinks=True`` and need the sink adapters (roadmap M9); until then the
   flag checks nothing extra.

**pending** lists manifests whose ``audit_written`` is ``pending`` or
``pending_partial`` and whose document has no row. A pending record is an
audit gap until ``audit-reconcile`` writes it, so it makes ``ok`` false.

Before the migration has run, ``audit_log`` is still the per-event table: the
legacy check covers it and the unified checks have nothing to check.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from sqlalchemy import select

from audit.digests import detail_sha256, io_sha256
from audit.writer import _sub_row_dict, attempt_hash, hash_entry
from storage.audit_log import (
    AuditAddendumRecord,
    AuditAttemptRecord,
    AuditLogRecord,
    AuditMetaRecord,
    AuditTraceIORecord,
)
from storage.db import async_session, ensure_schema

PENDING_STATES = ("pending", "pending_partial")
SECTIONS = ("main", "subrows", "addenda", "legacy", "trace_io", "pending")


@dataclass
class CheckResult:
    ok: bool = True
    checked: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def fail(self, reason: str, **where: Any) -> None:
        self.ok = False
        self.failures.append({"reason": reason, **where})


@dataclass
class VerifyReport:
    main: CheckResult = field(default_factory=CheckResult)
    subrows: CheckResult = field(default_factory=CheckResult)
    addenda: CheckResult = field(default_factory=CheckResult)
    legacy: CheckResult = field(default_factory=CheckResult)
    trace_io: CheckResult = field(default_factory=CheckResult)
    pending: CheckResult = field(default_factory=CheckResult)

    @property
    def ok(self) -> bool:
        return all(getattr(self, name).ok for name in SECTIONS)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **{name: asdict(getattr(self, name)) for name in SECTIONS}}


async def verify_audit_db(*, check_sinks: bool = False) -> VerifyReport:
    """Verify every section of the audit DB and return the report. Database errors propagate."""
    ensure_schema()
    report = VerifyReport()
    async with async_session() as session:
        migrated = (
            await session.execute(select(AuditMetaRecord.key).where(AuditMetaRecord.key == "migration").limit(1))
        ).first() is not None
        if migrated:
            rows = (await session.execute(select(AuditLogRecord).order_by(AuditLogRecord.seq))).scalars().all()
            adds = (
                await session.execute(
                    select(AuditAddendumRecord).order_by(AuditAddendumRecord.doc_id, AuditAddendumRecord.seq)
                )
            ).scalars().all()
            subs = (await session.execute(select(AuditAttemptRecord))).scalars().all()
            ios = (await session.execute(select(AuditTraceIORecord))).scalars().all()
        else:
            rows, adds, subs, ios = [], [], [], []
    _check_main(rows, report.main)
    _check_addenda(rows, adds, report.addenda)
    _check_subrows(rows, adds, subs, report.subrows)
    _check_trace_io(rows, adds, subs, ios, report.trace_io)
    await _check_legacy(report.legacy)
    _check_pending({r.doc_id for r in rows}, report.pending)
    if check_sinks:
        _check_external_sinks(report.trace_io)
    return report


# ---------------------------------------------------------------- checks


def _check_main(rows, out: CheckResult) -> None:
    prev = ""
    seen: set[str] = set()
    last_seq = None
    for row in rows:
        out.checked += 1
        where = {"doc_id": row.doc_id, "entry_id": row.entry_id, "seq": row.seq}
        if row.doc_id in seen:
            out.fail("duplicate_doc_id", **where)
        seen.add(row.doc_id)
        if last_seq is not None and row.seq <= last_seq:
            out.fail("seq_not_increasing", **where)
        last_seq = row.seq
        if row.prev_hash != prev:
            out.fail("prev_hash_mismatch", expected=prev, found=row.prev_hash, **where)
        detail = row.detail or {}
        expected = hash_entry(
            prev_hash=row.prev_hash, doc_id=row.doc_id, entry_id=row.entry_id, event=row.event,
            detail=detail, matter_id=row.matter_id, actor=row.actor, timestamp=row.timestamp,
        )
        if expected != row.entry_hash:
            out.fail("entry_hash_mismatch", **where)
        recovered = detail.get("recovered_from")
        if isinstance(recovered, dict) and "detail_sha256" in recovered:
            if recovered.get("detail_sha256") != detail_sha256(detail):
                out.fail("recovered_detail_sha_mismatch", **where)
        prev = row.entry_hash


def _check_addenda(rows, adds, out: CheckResult) -> None:
    by_doc = {r.doc_id: r for r in rows}
    prev_by_doc: dict[str, tuple[str, int]] = {}
    for add in adds:
        out.checked += 1
        where = {"doc_id": add.doc_id, "addendum_id": add.addendum_id, "seq": add.seq}
        row = by_doc.get(add.doc_id)
        if row is None:
            out.fail("addendum_without_row", **where)
            continue
        prev_hash, prev_seq = prev_by_doc.get(add.doc_id, (row.entry_hash, 0))
        if add.seq != prev_seq + 1:
            out.fail("seq_gap", expected=prev_seq + 1, **where)
        if add.prev_hash != prev_hash:
            out.fail("prev_hash_mismatch", expected=prev_hash, found=add.prev_hash, **where)
        expected = hash_entry(
            prev_hash=add.prev_hash, doc_id=add.doc_id, entry_id=add.addendum_id, event=add.kind,
            detail=add.detail or {}, matter_id=row.matter_id, actor=add.actor, timestamp=add.timestamp,
        )
        if expected != add.entry_hash:
            out.fail("entry_hash_mismatch", **where)
        prev_by_doc[add.doc_id] = (add.entry_hash, add.seq)


def _check_subrows(rows, adds, subs, out: CheckResult) -> None:
    rows_by_doc = {r.doc_id: r for r in rows}
    # parent id -> (doc_id, matter_id, actor, listed {attempt_id: entry_hash})
    parents: dict[str, tuple[str, str, str, dict[str, str]]] = {}
    for row in rows:
        parents[row.entry_id] = (row.doc_id, row.matter_id, row.actor, _listed(row.detail))
    for add in adds:
        row = rows_by_doc.get(add.doc_id)
        parents[add.addendum_id] = (add.doc_id, row.matter_id if row else "", add.actor, _listed(add.detail))
    stored: dict[str, Any] = {s.attempt_id: s for s in subs}
    for parent_id, (doc_id, _matter, _actor, listed) in parents.items():
        for attempt_id, listed_hash in listed.items():
            sub = stored.get(attempt_id)
            if sub is None or sub.parent_id != parent_id:
                out.checked += 1
                out.fail("missing_subrow", doc_id=doc_id, parent_id=parent_id, attempt_id=attempt_id)
            elif sub.entry_hash != listed_hash:
                out.fail("listed_hash_mismatch", doc_id=doc_id, parent_id=parent_id, attempt_id=attempt_id)
    for sub in subs:
        out.checked += 1
        where = {"doc_id": sub.doc_id, "parent_id": sub.parent_id, "attempt_id": sub.attempt_id}
        parent = parents.get(sub.parent_id)
        if parent is None:
            out.fail("orphan_subrow", **where)
            continue
        doc_id, matter_id, actor, listed = parent
        if sub.attempt_id not in listed:
            out.fail("unlisted_subrow", **where)
            continue
        if sub.doc_id != doc_id:
            out.fail("subrow_doc_mismatch", **where)
        if attempt_hash(_sub_row_dict(sub), matter_id=matter_id, actor=actor) != sub.entry_hash:
            out.fail("subrow_hash_mismatch", **where)


def _listed(detail: dict | None) -> dict[str, str]:
    items = (detail or {}).get("attempts") or []
    return {a["attempt_id"]: a.get("entry_hash", "") for a in items if isinstance(a, dict) and "attempt_id" in a}


def _visits(detail: dict | None) -> Iterator[dict]:
    for visit in (detail or {}).get("trace") or []:
        if isinstance(visit, dict):
            yield visit


def _check_trace_io(rows, adds, subs, ios, out: CheckResult) -> None:
    stored = {io.visit_id: io for io in ios}
    for io in ios:
        out.checked += 1
        if io_sha256(io.prompt, io.completion) != io.sha256:
            out.fail("io_content_mismatch", doc_id=io.doc_id, visit_id=io.visit_id)
    content_failed = {f["visit_id"] for f in out.failures}
    for owner in (*rows, *adds, *subs):
        for visit in _visits(owner.detail):
            sha = visit.get("io_sha256")
            if not sha:
                continue
            visit_id = visit.get("visit_id")
            out.checked += 1
            io = stored.get(visit_id)
            if io is None:
                out.fail("io_missing", doc_id=owner.doc_id, visit_id=visit_id)
            elif io.sha256 != sha and visit_id not in content_failed:
                out.fail("io_sha_mismatch", doc_id=owner.doc_id, visit_id=visit_id)


async def _check_legacy(out: CheckResult) -> None:
    """Today's per-document check on whichever table holds the per-event chains."""
    from schemas.audit import AuditLogEntry, verify_legacy_chain
    from storage.audit_log import event_log_model, get_audit_chain

    async with async_session() as session:
        model = await event_log_model(session)
        doc_ids = [r[0] for r in (await session.execute(select(model.doc_id).distinct())).all()]
    for doc_id in sorted(doc_ids):
        records = await get_audit_chain(doc_id)
        out.checked += 1
        entries = [
            AuditLogEntry(
                entry_id=r["entry_id"], doc_id=doc_id, matter_id=r.get("matter_id", ""), event=r["event"],
                actor=r["actor"], detail=r["detail"], prev_hash=r["prev_hash"], entry_hash=r["entry_hash"],
                timestamp=r["timestamp"],
            )
            for r in records
        ]
        if entries and not verify_legacy_chain(entries):
            out.fail("legacy_chain_broken", doc_id=doc_id, entries=len(entries))


def _check_pending(rows_doc_ids: set[str], out: CheckResult) -> None:
    from pipeline.bins import manifests_dir

    mdir = manifests_dir()
    if not mdir.is_dir():
        return
    for path in sorted(mdir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # not a manifest this check can read; the watcher reports it
        if not isinstance(data, dict) or data.get("audit_written") not in PENDING_STATES:
            continue
        out.checked += 1
        doc_id = str(data.get("doc_id") or path.stem)
        if doc_id not in rows_doc_ids:
            out.fail("pending_without_row", doc_id=doc_id, audit_written=data["audit_written"])


def _check_external_sinks(out: CheckResult) -> None:
    """External sink I/O check (``--check-sinks``). The adapters arrive in roadmap M9."""
    try:
        from audit import sinks  # noqa: F401
    except ImportError:
        return


__all__ = ["CheckResult", "VerifyReport", "verify_audit_db"]
