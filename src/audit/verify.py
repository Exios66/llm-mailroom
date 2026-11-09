"""Bounded verification of the unified audit storage (spec §6, roadmap M4).

This is deliberately a *storage* verifier. It proves the things this layer can
actually observe and says so when it cannot:

* **main** — the global terminal-row chain in ``audit_log`` (``seq`` order,
  ``prev_hash`` links, recomputed v2 hashes, unique ``doc_id``, and the
  ``recovered_from.detail_sha256`` binding when a recovered row carries one).
* **subrows** — every ``audit_attempts`` row recomputes, belongs to a known
  parent, and is listed by that parent's ``detail.attempts`` (missing, extra and
  edited rows all fail).
* **addenda** — each document's ``audit_addenda`` chain starts at the row hash,
  increments its per-document ``seq`` and links to the previous addendum.
* **legacy** — the previous release's per-document chain. After an explicit
  migration this reads ``audit_log_legacy``; before one it reads the live
  per-event ``audit_log``, so the verifier is useful in both states.
* **trace_io** — every stored ``audit_trace_io`` row matches ``io_sha256``.

**Not collected, and reported as such:** manifests parked with
``audit_written: pending`` and sink pending records belong to roadmap M9/M10 and
do not exist in this foundation layer. The ``pending`` section is marked
``collected=False``; it never claims proof of rows or I/O that were never
captured. External trace sinks are likewise unverified unless a future layer
implements them.

Hashing is unchanged: rows and addenda use
``audit.writer.hash_entry`` / ``schemas.audit.compute_audit_hash`` (v2, with the
legacy v1 fallback inside ``schemas.audit.verify_chain``), attempt sub-rows use
``audit.writer.attempt_hash``, and Dojo hashing is never reimplemented here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select

from audit.models import ADDENDUM_KINDS
from audit.writer import _take_migration_gate, attempt_hash, hash_entry
from schemas.audit import AuditLogEntry, verify_chain
from storage.audit_log import (
    AuditAddendumRecord,
    AuditAttemptRecord,
    AuditLegacyRecord,
    AuditLogRecord,
    AuditMetaRecord,
    AuditTraceIORecord,
)
from storage.db import async_session, begin_read_snapshot, ensure_schema

logger = structlog.get_logger(__name__)

MIGRATION_KEY = "migration"


def io_sha256(prompt: str | None, completion: str | None) -> str:
    """Canonical SHA-256 of one trace-I/O payload (spec §4.4).

    The future ``sql`` sink (roadmap M9) must use this helper, so the writer and
    the verifier cannot drift. ``None`` is part of the payload (a missing
    completion is distinct from an empty one).
    """
    payload = json.dumps(
        {"prompt": prompt, "completion": completion}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detail_sha256(detail: dict[str, Any]) -> str:
    """Canonical SHA-256 of a recovered row's ``detail`` (spec §5.6).

    The hash covers the detail object *without* its ``recovered_from`` key, so
    the recovery provenance can be added without changing the digest. The M10
    reconcile path must hash through this helper.
    """
    base = {k: v for k, v in (detail or {}).items() if k != "recovered_from"}
    payload = json.dumps(base, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CheckReport:
    """One verifier section: what it checked and every bounded failure."""

    ok: bool = True
    checked: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: False when this layer has no data source for the check (e.g. pending sinks).
    collected: bool = True
    note: str | None = None

    def fail(self, **failure: Any) -> None:
        self.failures.append(failure)
        self.ok = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "failures": self.failures,
            "collected": self.collected,
            "note": self.note,
        }


@dataclass
class VerifyReport:
    """The whole verification result; ``ok`` ignores uncollected sections."""

    main: CheckReport
    subrows: CheckReport
    addenda: CheckReport
    legacy: CheckReport
    trace_io: CheckReport
    pending: CheckReport
    migrated: bool

    @property
    def ok(self) -> bool:
        sections = (self.main, self.subrows, self.addenda, self.legacy, self.trace_io)
        return all(section.ok for section in sections if section.collected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "migrated": self.migrated,
            "main": self.main.as_dict(),
            "subrows": self.subrows.as_dict(),
            "addenda": self.addenda.as_dict(),
            "legacy": self.legacy.as_dict(),
            "trace_io": self.trace_io.as_dict(),
            "pending": self.pending.as_dict(),
        }


def _pending_report(check_sinks: bool) -> CheckReport:
    note = (
        "pending manifests and sink pending records are not collected in this "
        "foundation layer (roadmap M9/M10); no proof is claimed"
    )
    if check_sinks:
        note += "; external sinks are not implemented yet either"
    return CheckReport(collected=False, note=note)


async def _is_migrated(session) -> bool:
    found = await session.execute(
        select(AuditMetaRecord.key).where(AuditMetaRecord.key == MIGRATION_KEY).limit(1)
    )
    return found.first() is not None


def _row_hash(record) -> str:
    return hash_entry(
        prev_hash=record.prev_hash,
        doc_id=record.doc_id,
        entry_id=record.entry_id,
        event=record.event,
        detail=record.detail or {},
        matter_id=record.matter_id,
        actor=record.actor,
        timestamp=record.timestamp,
    )


def _addendum_hash(record, *, matter_id: str) -> str:
    return hash_entry(
        prev_hash=record.prev_hash,
        doc_id=record.doc_id,
        entry_id=record.addendum_id,
        event=record.kind,
        detail=record.detail or {},
        matter_id=matter_id,
        actor=record.actor,
        timestamp=record.timestamp,
    )


def _attempt_sub(row) -> dict[str, Any]:
    return {
        "attempt_id": row.attempt_id,
        "parent_id": row.parent_id,
        "doc_id": row.doc_id,
        "stage": row.stage,
        "attempt_no": row.attempt_no,
        "trigger": row.trigger,
        "outcome": row.outcome,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "detail": row.detail or {},
    }


async def _check_main(session, migrated: bool) -> CheckReport:
    if not migrated:
        return CheckReport(
            collected=False,
            note="migration marker absent: audit_log still holds the per-event rows (see legacy)",
        )
    rows = (
        (await session.execute(select(AuditLogRecord).order_by(AuditLogRecord.seq)))
        .scalars()
        .all()
    )
    report = CheckReport(checked=len(rows))
    seen: set[str] = set()
    previous_hash = ""
    previous_seq = 0
    for record in rows:
        if record.doc_id in seen:
            report.fail(kind="duplicate_doc_id", doc_id=record.doc_id)
        seen.add(record.doc_id)
        if record.seq != previous_seq + 1:
            report.fail(
                kind="seq_gap", doc_id=record.doc_id, seq=record.seq, expected=previous_seq + 1
            )
        if record.prev_hash != previous_hash:
            report.fail(
                kind="link_mismatch",
                doc_id=record.doc_id,
                seq=record.seq,
                expected=previous_hash,
                actual=record.prev_hash,
            )
        expected = _row_hash(record)
        if record.entry_hash != expected:
            report.fail(kind="hash_mismatch", doc_id=record.doc_id, seq=record.seq)
        recovered = (record.detail or {}).get("recovered_from")
        if isinstance(recovered, dict) and recovered.get("detail_sha256"):
            actual = detail_sha256(record.detail or {})
            if actual != recovered["detail_sha256"]:
                report.fail(
                    kind="recovered_detail_mismatch", doc_id=record.doc_id, seq=record.seq
                )
        previous_hash = record.entry_hash
        previous_seq = record.seq
    return report


async def _check_subrows(session, migrated: bool) -> CheckReport:
    if not migrated:
        return CheckReport(
            collected=False, note="migration marker absent: no unified sub-rows exist yet"
        )
    rows = (await session.execute(select(AuditLogRecord))).scalars().all()
    addenda = (await session.execute(select(AuditAddendumRecord))).scalars().all()
    attempts = (await session.execute(select(AuditAttemptRecord))).scalars().all()
    report = CheckReport(checked=len(attempts))

    parents: dict[str, tuple[Any, str, str]] = {}
    listed: dict[str, dict[str, Any]] = {}
    row_matter = {record.doc_id: record.matter_id for record in rows}
    for record in rows:
        parents[record.entry_id] = (record, record.matter_id, record.actor)
    for record in addenda:
        parents[record.addendum_id] = (
            record,
            row_matter.get(record.doc_id, ""),
            record.actor,
        )

    def _collect_refs(parent_id: str, detail: dict[str, Any] | None) -> None:
        listed[parent_id] = {}
        refs = (detail or {}).get("attempts") or []
        if not isinstance(refs, list):
            report.fail(kind="attempts_not_a_list", parent_id=parent_id)
            return
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("attempt_id"):
                report.fail(kind="attempt_ref_malformed", parent_id=parent_id)
                continue
            listed[parent_id][ref["attempt_id"]] = ref.get("entry_hash")

    for record in rows:
        _collect_refs(record.entry_id, record.detail)
    for record in addenda:
        _collect_refs(record.addendum_id, record.detail)

    by_attempt = {attempt.attempt_id: attempt for attempt in attempts}
    for attempt in attempts:
        parent = parents.get(attempt.parent_id)
        if parent is None:
            report.fail(
                kind="parent_missing", attempt_id=attempt.attempt_id, parent_id=attempt.parent_id
            )
            continue
        record, matter_id, actor = parent
        expected = attempt_hash(
            _attempt_sub(attempt), matter_id=matter_id, actor=actor
        )
        if attempt.entry_hash != expected:
            report.fail(kind="hash_mismatch", attempt_id=attempt.attempt_id)
        if attempt.doc_id != record.doc_id:
            report.fail(
                kind="doc_mismatch",
                attempt_id=attempt.attempt_id,
                expected=record.doc_id,
                actual=attempt.doc_id,
            )
        if listed.get(attempt.parent_id, {}).get(attempt.attempt_id) != attempt.entry_hash:
            report.fail(
                kind="not_listed_by_parent",
                attempt_id=attempt.attempt_id,
                parent_id=attempt.parent_id,
            )

    for parent_id, refs in listed.items():
        for attempt_id in refs:
            if attempt_id not in by_attempt:
                report.fail(kind="missing_subrow", parent_id=parent_id, attempt_id=attempt_id)
    return report


async def _check_addenda(session, migrated: bool) -> CheckReport:
    if not migrated:
        return CheckReport(
            collected=False, note="migration marker absent: no unified addenda exist yet"
        )
    rows = {
        record.doc_id: record
        for record in (await session.execute(select(AuditLogRecord))).scalars().all()
    }
    addenda = (
        (
            await session.execute(
                select(AuditAddendumRecord).order_by(
                    AuditAddendumRecord.doc_id, AuditAddendumRecord.seq
                )
            )
        )
        .scalars()
        .all()
    )
    report = CheckReport(checked=len(addenda))
    chains: dict[str, list] = {}
    for record in addenda:
        chains.setdefault(record.doc_id, []).append(record)
    for doc_id, chain in chains.items():
        row = rows.get(doc_id)
        if row is None:
            report.fail(kind="row_missing", doc_id=doc_id)
            continue
        previous_hash = row.entry_hash
        expected_seq = 1
        for record in chain:
            if record.seq != expected_seq:
                report.fail(
                    kind="seq_gap", doc_id=doc_id, seq=record.seq, expected=expected_seq
                )
            if record.prev_hash != previous_hash:
                report.fail(
                    kind="link_mismatch",
                    doc_id=doc_id,
                    seq=record.seq,
                    expected=previous_hash,
                    actual=record.prev_hash,
                )
            if record.kind not in ADDENDUM_KINDS:
                report.fail(
                    kind="unknown_kind",
                    doc_id=doc_id,
                    seq=record.seq,
                    addendum_kind=record.kind,
                )
            expected = _addendum_hash(record, matter_id=row.matter_id)
            if record.entry_hash != expected:
                report.fail(kind="hash_mismatch", doc_id=doc_id, seq=record.seq)
            previous_hash = record.entry_hash
            expected_seq += 1
    return report


def _utc(value: datetime) -> datetime:
    from datetime import timezone

    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _check_legacy(session, migrated: bool) -> CheckReport:
    model = AuditLegacyRecord if migrated else AuditLogRecord
    rows = (
        (
            await session.execute(
                select(model).order_by(model.doc_id, model.seq, model.timestamp, model.entry_id)
            )
        )
        .scalars()
        .all()
    )
    by_doc: dict[str, list] = {}
    for record in rows:
        by_doc.setdefault(record.doc_id, []).append(record)
    report = CheckReport(checked=len(by_doc))
    for doc_id, chain in by_doc.items():
        entries = [
            AuditLogEntry(
                entry_id=record.entry_id,
                doc_id=doc_id,
                matter_id=record.matter_id or "",
                event=record.event,
                actor=record.actor,
                detail=record.detail or {},
                prev_hash=record.prev_hash,
                entry_hash=record.entry_hash,
                timestamp=_utc(record.timestamp),
            )
            for record in chain
        ]
        if not verify_chain(entries):
            report.fail(kind="chain_broken", doc_id=doc_id, entries=len(chain))
    return report


async def _check_trace_io(session) -> CheckReport:
    rows = (await session.execute(select(AuditTraceIORecord))).scalars().all()
    report = CheckReport(checked=len(rows))
    for record in rows:
        if record.sha256 != io_sha256(record.prompt, record.completion):
            report.fail(kind="io_sha_mismatch", visit_id=record.visit_id, doc_id=record.doc_id)
    return report


async def _begin_read_snapshot(session) -> None:
    """Start ONE read-only snapshot before the first verifier query.

    SQLite's legacy transaction mode does not necessarily emit a physical
    ``BEGIN`` for a plain SELECT, so without this each statement could observe a
    different writer commit and produce false "broken chain" findings. An
    explicit ``BEGIN DEFERRED`` (never ``BEGIN IMMEDIATE``: the verifier must not
    take the write lock) pins one WAL read snapshot. Postgres gets an equivalent
    repeatable-read, read-only transaction. No initialization or migration runs
    inside this snapshot.
    """
    await begin_read_snapshot(session)
    # Postgres: hold the migration/rollback gate so the table set cannot be
    # swapped between the main, sub-row and addendum queries.
    await _take_migration_gate(session)


async def verify_audit_db(*, check_sinks: bool = False) -> VerifyReport:
    """Verify everything this storage layer can observe; see the module docstring.

    This is the offline/root verifier, so it calls ``ensure_schema()`` first;
    that may create missing tables and, only when ``MAILROOM_AUDIT_MIGRATE=1``,
    run the opt-in migration. Every verification query afterwards runs in ONE
    read-only snapshot (``_begin_read_snapshot``), so a writer committing
    between the main, sub-row and addendum queries cannot create a false
    failure. The HTTP viewer must not reuse this entry point's initialization.

    ``check_sinks`` is accepted for the future external-sink checks (roadmap
    M9); this layer records it as uncollected rather than pretending to verify
    sinks that do not exist. Database errors propagate.
    """
    ensure_schema()
    async with async_session() as session:
        await _begin_read_snapshot(session)
        migrated = await _is_migrated(session)
        main = await _check_main(session, migrated)
        subrows = await _check_subrows(session, migrated)
        addenda = await _check_addenda(session, migrated)
        legacy = await _check_legacy(session, migrated)
        trace_io = await _check_trace_io(session)
    pending = _pending_report(check_sinks)
    report = VerifyReport(
        main=main,
        subrows=subrows,
        addenda=addenda,
        legacy=legacy,
        trace_io=trace_io,
        pending=pending,
        migrated=migrated,
    )
    logger.info(
        "audit_verify_done",
        ok=report.ok,
        migrated=migrated,
        main_checked=main.checked,
        subrows_checked=subrows.checked,
        addenda_checked=addenda.checked,
        legacy_checked=legacy.checked,
        trace_io_checked=trace_io.checked,
    )
    return report
