"""Write the unified audit row, its sub-rows and its addenda (spec §4, §5.3).

* ``write_terminal_row`` takes the global ``audit_log:chain`` lock, reads the
  tail by ``seq``, links the new row to it and inserts the row and its attempt
  sub-rows in one transaction. If the document already has a row it writes a
  ``resumed`` addendum instead.
* ``write_addendum`` takes the per-document ``audit_log:<doc_id>`` lock and
  appends to the document's addendum chain, which starts at the row's hash.

Every hash is hash version 2 (``schemas.audit.compute_audit_hash``),
byte-identical with the dojo's ``format_audit_entry``. A sub-row is hashed
with ``entry_id = attempt_id`` and ``event = attempt:<stage>`` over an
envelope of its columns (``attempt_detail_envelope``), so its trigger,
outcome and times are covered too; the parent's ``detail.attempts`` lists the
sub-row hashes before the parent is hashed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from audit.models import (
    ADDENDUM_KINDS,
    EVENTS,
    Addendum,
    AttemptRow,
    AuditMigrationPending,
    AuditWriteError,
    NoAuditRow,
    TerminalRow,
    WriteResult,
)
from schemas.audit import compute_audit_hash
from storage.audit_log import (
    AuditAddendumRecord,
    AuditAttemptRecord,
    AuditLogRecord,
    AuditMetaRecord,
)
from storage.db import acquire_write_lock, async_session, ensure_schema

logger = structlog.get_logger(__name__)

CHAIN_LOCK = "audit_log:chain"
MAX_ATTEMPTS = 5


# ---------------------------------------------------------------- hashing


def _utc(ts: datetime | str | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _iso(ts: datetime | str | None) -> str | None:
    ts = _utc(ts)
    return ts.isoformat() if ts is not None else None


def hash_entry(
    *,
    prev_hash: str,
    doc_id: str,
    entry_id: str,
    event: str,
    detail: dict[str, Any],
    matter_id: str,
    actor: str,
    timestamp: datetime | str,
) -> str:
    """Hash version 2 of one entry. Naive timestamps (SQLite) are read as UTC."""
    return compute_audit_hash(
        prev_hash, doc_id, entry_id, event, detail,
        matter_id=matter_id, actor=actor, timestamp=_iso(timestamp),
    )


def attempt_detail_envelope(sub: dict[str, Any]) -> dict[str, Any]:
    """What a sub-row's hash covers besides its id: every column but the hash."""
    return {
        "parent_id": sub["parent_id"],
        "stage": sub["stage"],
        "attempt_no": sub["attempt_no"],
        "trigger": sub["trigger"],
        "outcome": sub.get("outcome"),
        "started_at": _iso(sub.get("started_at")),
        "ended_at": _iso(sub.get("ended_at")),
        "detail": sub.get("detail") or {},
    }


def attempt_hash(sub: dict[str, Any], *, matter_id: str, actor: str) -> str:
    """Hash of one sub-row. Sub-rows are not chained to each other (``prev_hash``
    is empty); the parent binds them by listing these hashes. The timestamp is
    the attempt's ``started_at`` (empty when unknown)."""
    return hash_entry(
        prev_hash="",
        doc_id=sub["doc_id"],
        entry_id=sub["attempt_id"],
        event=f"attempt:{sub['stage']}",
        detail=attempt_detail_envelope(sub),
        matter_id=matter_id,
        actor=actor,
        timestamp=_iso(sub.get("started_at")) or "",
    )


# ---------------------------------------------------------------- helpers


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


async def _require_migrated(session) -> None:
    found = await session.execute(
        select(AuditMetaRecord.key).where(AuditMetaRecord.key == "migration").limit(1)
    )
    if found.first() is None:
        raise AuditMigrationPending(
            "audit_meta has no migration record; run `python -m storage.audit_migrate` "
            "(Postgres: MAILROOM_AUDIT_MIGRATE=1 after pg_dump) before writing audit rows"
        )


async def _read_tail(session):
    """The last row of the global chain (``seq``, ``entry_hash``), or None."""
    return (
        await session.execute(
            select(AuditLogRecord.seq, AuditLogRecord.entry_hash)
            .order_by(desc(AuditLogRecord.seq))
            .limit(1)
        )
    ).first()


async def _existing_row(session, doc_id: str):
    return (
        await session.execute(select(AuditLogRecord).where(AuditLogRecord.doc_id == doc_id))
    ).scalar_one_or_none()


async def _ensure_catalog_parent(session, doc_id: str, matter_id: str, detail: dict) -> None:
    # audit_log.doc_id references documents.doc_id. A document that reaches a
    # terminal node is normally in the catalog already; a minimal row keeps
    # the FK satisfied when it is not (same rule as the per-event writer).
    from storage.catalog import DocumentRecord

    found = await session.execute(select(DocumentRecord.doc_id).where(DocumentRecord.doc_id == doc_id))
    if found.first() is None:
        session.add(
            DocumentRecord(
                doc_id=doc_id,
                matter_id=matter_id,
                original_filename=str(detail.get("original_filename") or ""),
                stage="processing",
            )
        )
        await session.flush()


def _build_sub_rows(
    attempts: list[AttemptRow], *, parent_id: str, doc_id: str, matter_id: str, actor: str
) -> tuple[list[AuditAttemptRecord], list[dict[str, Any]]]:
    records, listed = [], []
    for att in attempts:
        sub = {
            "attempt_id": att.attempt_id,
            "parent_id": parent_id,
            "doc_id": doc_id,
            "stage": att.stage,
            "attempt_no": att.attempt_no,
            "trigger": att.trigger,
            "outcome": att.outcome,
            "started_at": _utc(att.started_at),
            "ended_at": _utc(att.ended_at),
            "detail": dict(att.detail),
        }
        digest = attempt_hash(sub, matter_id=matter_id, actor=actor)
        records.append(AuditAttemptRecord(**sub, entry_hash=digest))
        listed.append(
            {"attempt_id": att.attempt_id, "stage": att.stage, "attempt_no": att.attempt_no, "entry_hash": digest}
        )
    return records, listed


def _is_retryable(exc: IntegrityError) -> bool:
    msg = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
    return "unique" in msg or "duplicate" in msg


# ---------------------------------------------------------------- writers


async def write_terminal_row(row: TerminalRow) -> WriteResult:
    """Write the document's one row, or a ``resumed`` addendum if it has one."""
    if row.event not in EVENTS:
        raise ValueError(f"unknown audit event {row.event!r}; expected one of {EVENTS}")
    ensure_schema()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with async_session() as session:
            await acquire_write_lock(session, CHAIN_LOCK)
            try:
                await _require_migrated(session)
                if await _existing_row(session, row.doc_id) is not None:
                    await session.rollback()
                    return await write_addendum(_as_resumed(row))
                await _ensure_catalog_parent(session, row.doc_id, row.matter_id, row.detail)

                tail = await _read_tail(session)
                seq = (tail.seq if tail else 0) + 1
                prev_hash = tail.entry_hash if tail else ""
                entry_id = _new_id("row")
                timestamp = datetime.now(timezone.utc)
                subs, listed = _build_sub_rows(
                    row.attempts, parent_id=entry_id, doc_id=row.doc_id,
                    matter_id=row.matter_id, actor=row.actor,
                )
                detail = {**row.detail, "attempts": listed}
                entry_hash = hash_entry(
                    prev_hash=prev_hash, doc_id=row.doc_id, entry_id=entry_id, event=row.event,
                    detail=detail, matter_id=row.matter_id, actor=row.actor, timestamp=timestamp,
                )
                session.add_all(subs)
                await session.flush()
                session.add(
                    AuditLogRecord(
                        entry_id=entry_id, doc_id=row.doc_id, matter_id=row.matter_id,
                        event=row.event, actor=row.actor, detail=detail, prev_hash=prev_hash,
                        entry_hash=entry_hash, seq=seq, timestamp=timestamp,
                    )
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if not _is_retryable(exc):
                    raise
                # Another writer took this seq (or this doc_id) between our tail
                # read and the insert: re-read under a fresh lock and relink.
                logger.warning("audit_write_conflict_retry", doc_id=row.doc_id, attempt=attempt)
                continue
            except BaseException:
                await session.rollback()
                raise
        logger.info("audit_row_written", doc_id=row.doc_id, entry_id=entry_id, seq=seq, event_name=row.event)
        return WriteResult(entry_id=entry_id, entry_hash=entry_hash, kind="row", seq=seq)
    raise AuditWriteError(
        f"audit row for {row.doc_id} not written: seq/doc_id conflict persisted after {MAX_ATTEMPTS} tries"
    )


def _as_resumed(row: TerminalRow) -> Addendum:
    # The row is immutable (§4.1): a second terminal write records what the
    # resumed run did as an addendum, keeping the event it would have written.
    return Addendum(
        doc_id=row.doc_id,
        kind="resumed",
        actor=row.actor,
        detail={**row.detail, "event": row.event},
        attempts=list(row.attempts),
    )


async def write_addendum(add: Addendum) -> WriteResult:
    """Append an addendum to a document that already has a row."""
    if add.kind not in ADDENDUM_KINDS:
        raise ValueError(f"unknown addendum kind {add.kind!r}; expected one of {ADDENDUM_KINDS}")
    ensure_schema()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with async_session() as session:
            await acquire_write_lock(session, f"audit_log:{add.doc_id}")
            try:
                await _require_migrated(session)
                row = await _existing_row(session, add.doc_id)
                if row is None:
                    raise NoAuditRow(f"document {add.doc_id} has no audit row; addenda need one")
                tail = (
                    await session.execute(
                        select(AuditAddendumRecord.seq, AuditAddendumRecord.entry_hash)
                        .where(AuditAddendumRecord.doc_id == add.doc_id)
                        .order_by(desc(AuditAddendumRecord.seq))
                        .limit(1)
                    )
                ).first()
                seq = (tail.seq if tail else 0) + 1
                prev_hash = tail.entry_hash if tail else row.entry_hash
                addendum_id = _new_id("add")
                timestamp = datetime.now(timezone.utc)
                subs, listed = _build_sub_rows(
                    add.attempts, parent_id=addendum_id, doc_id=add.doc_id,
                    matter_id=row.matter_id, actor=add.actor,
                )
                detail = {**add.detail, "attempts": listed} if listed else dict(add.detail)
                entry_hash = hash_entry(
                    prev_hash=prev_hash, doc_id=add.doc_id, entry_id=addendum_id, event=add.kind,
                    detail=detail, matter_id=row.matter_id, actor=add.actor, timestamp=timestamp,
                )
                session.add_all(subs)
                await session.flush()
                session.add(
                    AuditAddendumRecord(
                        addendum_id=addendum_id, doc_id=add.doc_id, kind=add.kind, seq=seq,
                        actor=add.actor, timestamp=timestamp, detail=detail,
                        prev_hash=prev_hash, entry_hash=entry_hash,
                    )
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if not _is_retryable(exc):
                    raise
                logger.warning("audit_write_conflict_retry", doc_id=add.doc_id, attempt=attempt, kind=add.kind)
                continue
            except BaseException:
                await session.rollback()
                raise
        logger.info("audit_addendum_written", doc_id=add.doc_id, addendum_id=addendum_id, kind=add.kind, seq=seq)
        return WriteResult(entry_id=addendum_id, entry_hash=entry_hash, kind="addendum", seq=seq)
    raise AuditWriteError(
        f"addendum for {add.doc_id} not written: seq conflict persisted after {MAX_ATTEMPTS} tries"
    )


# ---------------------------------------------------------------- reader


def _sub_row_dict(rec: AuditAttemptRecord) -> dict[str, Any]:
    return {
        "attempt_id": rec.attempt_id,
        "parent_id": rec.parent_id,
        "doc_id": rec.doc_id,
        "stage": rec.stage,
        "attempt_no": rec.attempt_no,
        "trigger": rec.trigger,
        "outcome": rec.outcome,
        "started_at": _iso(rec.started_at),
        "ended_at": _iso(rec.ended_at),
        "detail": rec.detail or {},
        "entry_hash": rec.entry_hash,
    }


async def get_row(doc_id: str) -> dict[str, Any] | None:
    """The document's row with its sub-rows and addenda, or None.

    Timestamps come back as UTC ISO strings, the form they were hashed in.
    """
    ensure_schema()
    async with async_session() as session:
        row = await _existing_row(session, doc_id)
        if row is None:
            return None
        subs = (
            await session.execute(
                select(AuditAttemptRecord)
                .where(AuditAttemptRecord.doc_id == doc_id)
                .order_by(AuditAttemptRecord.stage, AuditAttemptRecord.attempt_no, AuditAttemptRecord.attempt_id)
            )
        ).scalars().all()
        adds = (
            await session.execute(
                select(AuditAddendumRecord)
                .where(AuditAddendumRecord.doc_id == doc_id)
                .order_by(AuditAddendumRecord.seq)
            )
        ).scalars().all()
    return {
        "entry_id": row.entry_id,
        "doc_id": row.doc_id,
        "matter_id": row.matter_id,
        "seq": row.seq,
        "event": row.event,
        "actor": row.actor,
        "timestamp": _iso(row.timestamp),
        "prev_hash": row.prev_hash,
        "entry_hash": row.entry_hash,
        "detail": row.detail or {},
        "sub_rows": [_sub_row_dict(s) for s in subs if s.parent_id == row.entry_id],
        "addenda": [
            {
                "addendum_id": a.addendum_id,
                "kind": a.kind,
                "seq": a.seq,
                "actor": a.actor,
                "timestamp": _iso(a.timestamp),
                "prev_hash": a.prev_hash,
                "entry_hash": a.entry_hash,
                "detail": a.detail or {},
                "sub_rows": [_sub_row_dict(s) for s in subs if s.parent_id == a.addendum_id],
            }
            for a in adds
        ],
    }
