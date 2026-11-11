"""Write the unified audit row, its sub-rows and its addenda (spec §4, §5.3).

* ``write_terminal_row`` takes the global ``audit_log:chain`` lock, reads the
  tail by ``seq``, links the new row to it and inserts the row and its attempt
  sub-rows in one transaction. If the document already has a row it writes a
  ``resumed`` addendum instead.
* ``write_addendum`` takes the per-document ``doc_lock_key(doc_id)`` lock
  (``audit_doc:<doc_id>``, kept apart from the global ``audit_log:chain`` key
  even for a document named ``chain``) and appends to the document's addendum
  chain, which starts at the row's hash.

Every hash is hash version 2 (``schemas.audit.compute_audit_hash``),
byte-identical with the dojo's ``archive_entry_hash``. The dojo's
``format_audit_entry`` builds and normalizes its detail object before hashing
with ``archive_entry_hash``. ``seq`` is not hashed. A sub-row is hashed
with ``entry_id = attempt_id`` and ``event = attempt:<stage>`` over an
envelope of its columns (``attempt_detail_envelope``), so its trigger,
outcome and times are covered too; the parent's ``detail.attempts`` lists the
sub-row hashes before the parent is hashed.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import desc, select, text
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
    doc_lock_key,
)
from storage.db import (
    acquire_write_lock,
    async_session,
    begin_read_snapshot,
    ensure_schema,
    run_write_with_retry,
)

logger = structlog.get_logger(__name__)

CHAIN_LOCK = "audit_log:chain"
MAX_ATTEMPTS = 5


# ---------------------------------------------------------------- hashing


def _utc(ts: datetime | str | None) -> datetime | None:
    """Normalize to UTC, treating naive values as UTC and preserving ``None``.

    The empty string (the stored unknown-start shape) normalizes like ``None``.
    Raise ``ValueError`` if a string is not a valid ISO datetime.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _iso(ts: datetime | str | None) -> str | None:
    """Return the UTC ISO datetime or ``None``; invalid strings raise ``ValueError``."""
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
    """Return the version 2 entry hash after normalizing its timestamp to UTC.

    Naive timestamps (SQLite) are read as UTC. Invalid non-empty ISO datetime strings
    raise ``ValueError``; the empty string is preserved for unknown attempt times.
    """
    return compute_audit_hash(
        prev_hash, doc_id, entry_id, event, detail,
        matter_id=matter_id, actor=actor, timestamp="" if timestamp == "" else _iso(timestamp),
    )


def attempt_detail_envelope(sub: dict[str, Any]) -> dict[str, Any]:
    """Build the attempt hash's detail payload, normalizing optional times to UTC."""
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
    the attempt's ``started_at``, or empty when the start time is unknown.
    Invalid non-empty timestamps raise ``ValueError`` during normalization.
    """
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


def _snapshot_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-copy a caller's detail before it is hashed and stored.

    The hash and the persisted JSON must describe the same bytes. A caller (or
    a token callback) that mutates a nested object between the hash and the
    flush would otherwise store a detail that no longer matches its hash.
    """
    return copy.deepcopy(dict(detail or {}))


def _snapshot_attempts(attempts: list[AttemptRow]) -> list[AttemptRow]:
    """Deep-copy each attempt (including its nested detail) without mutation."""
    return [replace(attempt, detail=copy.deepcopy(dict(attempt.detail))) for attempt in attempts]


async def _take_migration_gate(session) -> None:
    """Take the shared migration/rollback gate on Postgres before reading or writing.

    Migration and rollback hold ``LOCK_KEY`` exclusively; a writer/reader that
    takes it shared first cannot interleave with the table swap (and cannot
    deadlock on the reverse lock order). SQLite serializes with ``BEGIN
    IMMEDIATE`` for writers and a read snapshot for readers, so it needs no
    advisory equivalent.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    from storage.audit_migrate import LOCK_KEY

    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtext(:key))"), {"key": LOCK_KEY}
    )


def _reidentify_attempts(
    attempts: list[AttemptRow], *, parent_id: str
) -> list[AttemptRow]:
    """Return copies of ``attempts`` with identities unique to ``parent_id``.

    The caller's ``AttemptRow`` objects are never mutated. The new id is
    deterministic from the parent and the attempt's position/stage/number, and
    the original id is kept in ``detail.original_attempt_id`` so provenance is
    explicit rather than silently dropped.
    """
    from dataclasses import replace

    reidentified: list[AttemptRow] = []
    for index, attempt in enumerate(attempts):
        detail = copy.deepcopy(dict(attempt.detail))
        detail["original_attempt_id"] = attempt.attempt_id
        reidentified.append(
            replace(
                attempt,
                attempt_id=f"{parent_id}:{index}:{attempt.stage}:{attempt.attempt_no}",
                detail=detail,
            )
        )
    return reidentified


async def _require_migrated(session) -> None:
    """Raise ``AuditMigrationPending`` when the migration marker is absent."""
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
    """Create and flush a minimal processing-stage catalog row only if absent."""
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
    """Build unsaved attempt records and their hash references for the parent detail."""
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
            "detail": copy.deepcopy(dict(att.detail)),
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


async def write_terminal_row(
    row: TerminalRow, *, entry_id: str | None = None, recovered_from: dict[str, Any] | None = None
) -> WriteResult:
    """Commit a terminal row and its attempts, returning its entry id, hash, and sequence.

    A new row joins the global chain and creates a minimal catalog document
    if needed. Its ``detail.attempts`` is replaced with attempt hash references.
    An existing row is preserved; the call writes a ``resumed`` addendum with
    the requested event in ``detail.event`` and returns its identity instead.

    ``entry_id`` and ``recovered_from`` are for ``audit-reconcile`` (§5.6): the
    row keeps the id its pending record was captured under, so its attempt
    hashes and ``detail`` are the captured ones, and ``detail.recovered_from``
    is added after the attempts are listed.

    Raise ``ValueError`` for an unknown event or invalid attempt timestamp,
    ``AuditMigrationPending`` if the migration marker is absent, and
    ``AuditWriteError`` after five attempts encounter uniqueness conflicts.
    Attempt start times may be ``None`` (persisted as unknown). The caller's
    ``detail`` and attempt objects are deep-copied before hashing, so later
    mutation cannot make the stored row disagree with its hash. Transient
    SQLite lock errors (``BEGIN IMMEDIATE`` -> ``database is locked``) retry
    the whole transaction in a fresh session under the shared PR #203 backoff,
    bounded to the same attempt budget; other database errors propagate
    immediately. Failed transactions are rolled back.
    """
    if row.event not in EVENTS:
        raise ValueError(f"unknown audit event {row.event!r}; expected one of {EVENTS}")
    ensure_schema()
    snapshot_detail = _snapshot_detail(row.detail)
    snapshot_attempts = _snapshot_attempts(row.attempts)
    # PR #203's retry wrapper: contention on SQLite's writer lock raises
    # OperationalError before the transaction body runs. Retrying here (not
    # only uniqueness conflicts below) keeps a busy writer queue from turning
    # a valid terminal write into a lost row. ``op`` opens its own session per
    # attempt, so every retry re-reads the chain tail.
    return await run_write_with_retry(
        lambda: _write_terminal_row_once(
            row,
            detail=snapshot_detail,
            attempts=snapshot_attempts,
            entry_id=entry_id,
            recovered_from=_snapshot_detail(recovered_from) if recovered_from is not None else None,
        ),
        attempts=MAX_ATTEMPTS,
    )


async def _write_terminal_row_once(
    row: TerminalRow,
    *,
    detail: dict[str, Any],
    attempts: list[AttemptRow],
    entry_id: str | None = None,
    recovered_from: dict[str, Any] | None = None,
) -> WriteResult:
    """One bounded transaction attempt loop for a new terminal row."""
    # A reconcile write keeps the entry id its pending record was captured
    # under; an ordinary write mints a fresh one.
    preset_id = entry_id
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with async_session() as session:
            try:
                # Lock acquisition belongs INSIDE the try: SQLite's
                # ``BEGIN IMMEDIATE`` raises ``OperationalError`` under writer
                # contention, and the wrapper above must see it (and this
                # handler must roll the session back) like any other failure.
                await acquire_write_lock(session, CHAIN_LOCK)
                await _require_migrated(session)
                if await _existing_row(session, row.doc_id) is not None:
                    await session.rollback()
                    resumed = _as_resumed(row, detail=detail, attempts=attempts)
                    # No nested retry wrapper: a transient lock error here
                    # bubbles to the terminal row's wrapper, which replays the
                    # whole write (including this addendum) in a fresh session.
                    return await _write_addendum_once(
                        resumed, detail=resumed.detail, attempts=resumed.attempts
                    )
                await _ensure_catalog_parent(
                    session, row.doc_id, row.matter_id, detail
                )

                tail = await _read_tail(session)
                seq = (tail.seq if tail else 0) + 1
                prev_hash = tail.entry_hash if tail else ""
                entry_id = preset_id or _new_id("row")
                timestamp = datetime.now(timezone.utc)
                subs, listed = _build_sub_rows(
                    attempts, parent_id=entry_id, doc_id=row.doc_id,
                    matter_id=row.matter_id, actor=row.actor,
                )
                row_detail = {**detail, "attempts": listed}
                if recovered_from is not None:
                    # Added after the attempts are listed, so the recovered
                    # row's hash covers exactly what the pending record held
                    # plus this provenance block.
                    row_detail["recovered_from"] = copy.deepcopy(recovered_from)
                entry_hash = hash_entry(
                    prev_hash=prev_hash, doc_id=row.doc_id, entry_id=entry_id, event=row.event,
                    detail=row_detail, matter_id=row.matter_id, actor=row.actor, timestamp=timestamp,
                )
                session.add_all(subs)
                await session.flush()
                session.add(
                    AuditLogRecord(
                        entry_id=entry_id, doc_id=row.doc_id, matter_id=row.matter_id,
                        event=row.event, actor=row.actor, detail=row_detail, prev_hash=prev_hash,
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


def _as_resumed(
    row: TerminalRow, *, detail: dict[str, Any], attempts: list[AttemptRow]
) -> Addendum:
    """Convert a repeat terminal write to an addendum retaining the requested event.

    ``detail`` and ``attempts`` are the writer's deep-copied snapshot, so the
    caller's objects are never reused or mutated.
    """
    # The row is immutable (§4.1): a second terminal write records what the
    # resumed run did as an addendum, keeping the event it would have written.
    return Addendum(
        doc_id=row.doc_id,
        kind="resumed",
        actor=row.actor,
        detail={**detail, "event": row.event},
        attempts=list(attempts),
        # The row is immutable, so a repeat terminal write must not reuse the
        # original attempt ids: they are the primary keys of the row's own
        # sub-rows. The addendum gets new identities scoped to itself.
        reidentify_attempts=True,
    )


async def write_addendum(add: Addendum) -> WriteResult:
    """Commit an addendum and its attempts, returning its entry id, hash, and sequence.

    The per-document chain starts at the terminal row's hash; its sequence
    starts at 1. The terminal row supplies the matter id and stays unchanged.
    Attempt hash references always replace ``detail.attempts`` when attempts
    are supplied, including an empty list; an inherited ``attempts`` key is
    cleared when no attempts are supplied so a parent never lists hashes that
    have no matching sub-rows.

    Raise ``ValueError`` for an unknown kind or invalid attempt timestamp,
    ``AuditMigrationPending`` if the migration marker is absent, ``NoAuditRow``
    if the document has no terminal row, and ``AuditWriteError`` after five
    attempts encounter uniqueness conflicts. A ``resumed`` addendum built from a
    repeat terminal write gets new attempt identities (the original ids are the
    row's sub-row primary keys); see ``_as_resumed``. On Postgres the shared
    migration/rollback gate is taken before the document lock, so a concurrent
    rollback cannot deadlock the write. Transient SQLite lock errors retry the
    whole transaction in a fresh session under the shared PR #203 backoff,
    bounded to the same attempt budget; other database errors propagate
    immediately. Failed transactions are rolled back.
    """
    if add.kind not in ADDENDUM_KINDS:
        raise ValueError(f"unknown addendum kind {add.kind!r}; expected one of {ADDENDUM_KINDS}")
    ensure_schema()
    snapshot_detail = _snapshot_detail(add.detail)
    snapshot_attempts = _snapshot_attempts(add.attempts)
    # Same PR #203 wrapper as the terminal row: lock contention is a transient
    # database condition, not a lost addendum. Every attempt opens a new
    # session and re-reads the per-document tail.
    return await run_write_with_retry(
        lambda: _write_addendum_once(
            add, detail=snapshot_detail, attempts=snapshot_attempts
        ),
        attempts=MAX_ATTEMPTS,
    )


async def _write_addendum_once(
    add: Addendum, *, detail: dict[str, Any], attempts: list[AttemptRow]
) -> WriteResult:
    """One bounded transaction attempt loop for an addendum."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with async_session() as session:
            try:
                # Gate first: migration/rollback lock rows in the opposite
                # order, so taking the shared gate before the document lock
                # keeps the lock order consistent and prevents a deadlock.
                # Both live INSIDE the try so a lock failure rolls the session
                # back and reaches the retry wrapper.
                await _take_migration_gate(session)
                await acquire_write_lock(session, doc_lock_key(add.doc_id))
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
                sub_rows = (
                    _reidentify_attempts(attempts, parent_id=addendum_id)
                    if add.reidentify_attempts
                    else attempts
                )
                subs, listed = _build_sub_rows(
                    sub_rows, parent_id=addendum_id, doc_id=add.doc_id,
                    matter_id=row.matter_id, actor=add.actor,
                )
                addendum_detail = dict(detail)
                if listed or "attempts" in addendum_detail:
                    # Always replace inherited references with the real list.
                    addendum_detail["attempts"] = listed
                entry_hash = hash_entry(
                    prev_hash=prev_hash, doc_id=add.doc_id, entry_id=addendum_id, event=add.kind,
                    detail=addendum_detail, matter_id=row.matter_id, actor=add.actor, timestamp=timestamp,
                )
                session.add_all(subs)
                await session.flush()
                session.add(
                    AuditAddendumRecord(
                        addendum_id=addendum_id, doc_id=add.doc_id, kind=add.kind, seq=seq,
                        actor=add.actor, timestamp=timestamp, detail=addendum_detail,
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
    Each parent has its own ``sub_rows``, ordered by stage, attempt number,
    and attempt id; addenda are ordered by their per-document sequence.

    Raise ``AuditMigrationPending`` when the migration marker is absent: the
    live ``audit_log`` then holds per-event rows and there is no unified row to
    return (reading one would either raise ``MultipleResultsFound`` or present
    a legacy event as a row). All four reads run in ONE read-only snapshot, so
    a concurrent addendum cannot produce ``detail.attempts`` hashes without
    their ``sub_rows``. Schema initialization may create tables/migrate only
    with the explicit opt-in; database errors propagate.
    """
    ensure_schema()
    async with async_session() as session:
        await begin_read_snapshot(session)
        await _take_migration_gate(session)
        await _require_migrated(session)
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


# ---------------------------------------------------------------- write-failure fallback (§5.6)

#: Attempts to commit a terminal row before it is parked in the sinks, and the waits between them.
COMMIT_ATTEMPTS = 3
RETRY_DELAYS_S = (0.1, 0.3)


@dataclass(frozen=True)
class CommitOutcome:
    """How a terminal row ended: ``written``, ``pending``, ``pending_partial`` or ``gap``."""

    status: str
    result: WriteResult | None = None
    refs: tuple[dict[str, Any], ...] = ()


def _prepare(row: TerminalRow, entry_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The row's ``detail`` and sub-rows exactly as ``write_terminal_row`` would store them under ``entry_id``."""
    records, listed = _build_sub_rows(
        row.attempts, parent_id=entry_id, doc_id=row.doc_id, matter_id=row.matter_id, actor=row.actor,
    )
    subs = [_sub_row_dict(rec) for rec in records]
    return {**copy.deepcopy(row.detail), "attempts": listed}, subs


def pending_record(row: TerminalRow, *, entry_id: str | None = None) -> dict[str, Any]:
    """The complete pending record of §5.6 step 1, captured under a fixed ``entry_id``.

    ``detail`` is the stored form (``detail.attempts`` lists the sub-row hashes),
    so ``detail_sha256`` is what the verifier recomputes on the recovered row.
    """
    from audit.digests import detail_sha256

    entry_id = entry_id or _new_id("row")
    detail, subs = _prepare(row, entry_id)
    return {
        "entry_id": entry_id, "doc_id": row.doc_id, "matter_id": row.matter_id, "event": row.event,
        "actor": row.actor, "detail": detail, "sub_rows": subs,
        "sub_row_hashes": [s["entry_hash"] for s in subs], "detail_sha256": detail_sha256(detail),
        "captured_at": datetime.now(timezone.utc).isoformat(), "complete": True,
    }


def _decisions_view(visits: Any) -> list[dict[str, Any]]:
    keep = ("visit_id", "node", "attempt_id", "status", "outcome", "decisions")
    return [{k: v.get(k) for k in keep} for v in visits or [] if isinstance(v, dict)]


def decisions_only_record(row: TerminalRow, full: dict[str, Any]) -> dict[str, Any]:
    """The decisions-only copy for a sink whose limit the full record exceeds (§5.6 step 1).

    The trace keeps each visit's identity and ``decisions``; sub-rows keep their
    nodes and decisions; extraction data, I/O and scoring detail are dropped. It
    carries its own ``detail_sha256`` and the full record's as ``full_detail_sha256``.
    """
    from audit.digests import detail_sha256

    detail = copy.deepcopy(row.detail)
    detail["trace"] = _decisions_view(detail.get("trace"))
    detail["extraction"] = None
    detail["scoring"] = None
    attempts = [
        AttemptRow(
            attempt_id=a.attempt_id, stage=a.stage, attempt_no=a.attempt_no, trigger=a.trigger,
            outcome=a.outcome, started_at=a.started_at, ended_at=a.ended_at,
            detail={"nodes": (a.detail or {}).get("nodes"), "decisions": (a.detail or {}).get("decisions")},
        )
        for a in row.attempts
    ]
    small = TerminalRow(doc_id=row.doc_id, matter_id=row.matter_id, event=row.event, actor=row.actor,
                        detail=detail, attempts=attempts)
    stored, subs = _prepare(small, full["entry_id"])
    return {
        **{k: full[k] for k in ("entry_id", "doc_id", "matter_id", "event", "actor", "captured_at")},
        "detail": stored, "sub_rows": subs, "sub_row_hashes": [s["entry_hash"] for s in subs],
        "detail_sha256": detail_sha256(stored), "complete": False,
        "full_detail_sha256": full["detail_sha256"],
    }


async def commit_or_park(row: TerminalRow) -> CommitOutcome:
    """Commit the row; after ``COMMIT_ATTEMPTS`` failures hand it to the trace sinks (§5.6).

    ``AuditMigrationPending`` counts as a failure, so the record waits for
    ``audit-reconcile`` after the migration. Never raises for a write failure:
    the run that produced the row still succeeds.
    """
    import asyncio

    from audit import sinks

    error: BaseException | None = None
    for attempt in range(1, COMMIT_ATTEMPTS + 1):
        try:
            return CommitOutcome("written", await write_terminal_row(row))
        except ValueError:
            raise  # a malformed row is a bug, not a write failure
        except Exception as exc:  # noqa: BLE001 - any failure to commit takes the fallback
            error = exc
            logger.warning("audit_row_commit_failed", doc_id=row.doc_id, attempt=attempt,
                           error=f"{type(exc).__name__}: {exc}")
            if attempt < COMMIT_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)])
    full = pending_record(row)
    refs = tuple(sinks.capture_pending(full, partial=lambda: decisions_only_record(row, full)))
    detail = {"doc_id": row.doc_id, "event_name": row.event, "refs": list(refs),
              "error": f"{type(error).__name__}: {error}" if error else None}
    if any(r["complete"] for r in refs):
        logger.info("audit_pending", **detail)
        return CommitOutcome("pending", refs=refs)
    if refs:
        logger.warning("audit_pending_partial", **detail)
        return CommitOutcome("pending_partial", refs=refs)
    logger.error("audit_gap", **detail)
    return CommitOutcome("gap")
