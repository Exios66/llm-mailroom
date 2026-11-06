import structlog
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, Text, Integer, ForeignKey, Index, select, desc
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, acquire_write_lock, async_session, ensure_schema

logger = structlog.get_logger(__name__)


class AuditLogRecord(Base):
    """One hash-chained row per document (unified audit row, spec §4.1).

    ``doc_id`` is unique and ``seq`` is one sequence across the database; the
    row is never updated after it is written. The pre-migration per-event
    chains live in :class:`AuditLegacyRecord` (``audit_log_legacy``).
    """

    __tablename__ = "audit_log"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(128),
        # A-5: an audit entry must belong to a catalog document (ON DELETE
        # RESTRICT — documents are never deleted, so the FK only prevents
        # orphaned chains).
        ForeignKey("documents.doc_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    matter_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(256), default="")
    entry_hash: Mapped[str] = mapped_column(String(256), default="")
    # Global chain position, assigned under the ``audit_log:chain`` lock. It is
    # not part of the hash. No default: a writer that forgets it must fail on
    # the unique index rather than store a shared 0.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AuditLegacyRecord(Base):
    """``audit_log_legacy``: the previous release's ``audit_log``, rows unchanged.

    Per-event chains: many rows per ``doc_id`` with ``seq`` counted per
    document, so neither is unique here.
    """

    __tablename__ = "audit_log_legacy"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("documents.doc_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    matter_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(256), default="")
    entry_hash: Mapped[str] = mapped_column(String(256), default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AuditAttemptRecord(Base):
    """``audit_attempts``: sub-rows of a document row or a ``resumed`` addendum (§4.2).

    Written in the same transaction as the parent, whose ``detail.attempts``
    lists each ``entry_hash``. ``parent_id`` is the row's ``entry_id`` or an
    addendum id, so it carries no foreign key.
    """

    __tablename__ = "audit_attempts"

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)  # classification | extraction | judge
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # initial | retry | arbiter_rescue | human_correction | transient | resume
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    entry_hash: Mapped[str] = mapped_column(String(256), default="")


class AuditAddendumRecord(Base):
    """``audit_addenda``: what happens to a document after its row (§4.3).

    ``seq`` counts per document. The first addendum's ``prev_hash`` is the
    row's ``entry_hash``; each later one links to the previous addendum.
    """

    __tablename__ = "audit_addenda"
    __table_args__ = (Index("ix_audit_addenda_doc_seq", "doc_id", "seq", unique=True),)

    addendum_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # rescore | resumed | review_decision | relations_linked | recovered_detail
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(256), default="")
    entry_hash: Mapped[str] = mapped_column(String(256), default="")


class AuditMetaRecord(Base):
    """``audit_meta``: key/value facts about the audit store (§4.5).

    The ``migration`` key holds ``{migrated_at, legacy_rows,
    legacy_tail_hashes_sha256}`` once the legacy migration has run; its
    presence is what makes the migration skip.
    """

    __tablename__ = "audit_meta"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditTraceIORecord(Base):
    """``audit_trace_io``: prompts and completions of the ``sql`` trace sink (§4.5)."""

    __tablename__ = "audit_trace_io"

    visit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def event_log_model(session):
    """The table the per-event writer and readers use right now.

    INTERIM BRIDGE, removed with the per-event writers (roadmap M7/M8, M11).
    ``audit_log`` holds one row per document once the legacy migration has run
    (unique ``doc_id`` and global ``seq``), so ``write_audit_entry`` and the
    per-document readers move to ``audit_log_legacy``. Until then (Postgres
    without ``MAILROOM_AUDIT_MIGRATE=1``, or a migration that aborted) the old
    ``audit_log`` is still the live per-event table and they keep using it.
    """
    row = await session.execute(
        select(AuditMetaRecord.key).where(AuditMetaRecord.key == "migration").limit(1)
    )
    return AuditLegacyRecord if row.first() else AuditLogRecord


def _link_to_tail(entry, tail) -> None:
    """Re-link ``entry`` onto the chain tail read under the write lock.

    Callers compute prev_hash (and the hash) from an earlier, unlocked read;
    if another writer appended in between, that prev_hash is stale. Re-point
    it at the real tail and keep the timestamp strictly after the tail's
    (verify_chain orders by timestamp), re-hashing only when something moved
    so an up-to-date entry is stored byte-identical.
    """
    from datetime import timedelta

    from schemas.audit import compute_audit_hash

    tail_hash = tail.entry_hash if tail else ""
    ts = entry.timestamp
    if tail is not None and tail.timestamp is not None:
        tail_ts = tail.timestamp
        if tail_ts.tzinfo is None:  # SQLite stores naive UTC
            tail_ts = tail_ts.replace(tzinfo=timezone.utc)
        if ts <= tail_ts:
            ts = tail_ts + timedelta(microseconds=1)
    if entry.prev_hash == tail_hash and ts == entry.timestamp:
        return
    logger.info("audit_entry_relinked", entry_id=entry.entry_id, doc_id=entry.doc_id)
    entry.prev_hash = tail_hash
    entry.timestamp = ts
    entry.entry_hash = compute_audit_hash(
        entry.prev_hash, entry.doc_id, entry.entry_id, entry.event, entry.detail,
        matter_id=entry.matter_id, actor=entry.actor, timestamp=ts,
    )


async def write_audit_entry(entry) -> AuditLogRecord | AuditLegacyRecord:
    """Commit a per-event entry and return its stored record.

    After migration, events go to ``audit_log_legacy``. Create a minimal
    catalog document if absent and assign the next per-document sequence.
    The supplied entry's previous hash, timestamp, and hash may be updated
    to link it to the current tail. Database errors propagate.
    """
    ensure_schema()
    from schemas.audit import AuditLogEntry
    async with async_session() as session:
        # Serialize the whole read-tail-then-append for this doc: without a
        # write lock, concurrent appenders read the same tail/max(seq) and
        # fork the chain (duplicate prev_hash and seq).
        await acquire_write_lock(session, f"audit_log:{entry.doc_id}")
        Model = await event_log_model(session)

        # A-5: the FK audit_log.doc_id -> documents.doc_id is enforced per
        # connection. Audit-first flows (ingest writes its audit entry before
        # the catalog row exists) must ensure the parent row, or the append
        # fails the FK. A minimal processing-stage row satisfies the FK and
        # gets updated by the catalog writer moments later.
        from storage.catalog import DocumentRecord
        from sqlalchemy import select as _select

        parent = await session.execute(
            _select(DocumentRecord.doc_id).where(DocumentRecord.doc_id == entry.doc_id)
        )
        if parent.first() is None:
            session.add(
                DocumentRecord(
                    doc_id=entry.doc_id,
                    matter_id=entry.matter_id,
                    original_filename=entry.detail.get("original_filename", ""),
                    stage="processing",
                )
            )
            await session.flush()

        # A-3: deterministic chain order — the next seq is max(seq)+1 for this
        # doc, read under the write lock taken above, so concurrent appends
        # cannot interleave.
        tail = (
            await session.execute(
                select(Model.seq, Model.entry_hash, Model.timestamp)
                .where(Model.doc_id == entry.doc_id)
                .order_by(desc(Model.seq), desc(Model.timestamp))
                .limit(1)
            )
        ).first()
        next_seq = ((tail.seq or 0) if tail else 0) + 1
        _link_to_tail(entry, tail)
        record = Model(
            entry_id=entry.entry_id,
            doc_id=entry.doc_id,
            matter_id=entry.matter_id,
            event=entry.event,
            actor=entry.actor,
            detail=entry.detail,
            prev_hash=entry.prev_hash,
            entry_hash=entry.entry_hash,
            seq=next_seq,
            timestamp=entry.timestamp,
        )
        session.add(record)
        await session.commit()
        logger.info("audit_entry_written", entry_id=entry.entry_id, event_name=entry.event)
        return record


async def get_audit_chain(doc_id: str) -> list[dict]:
    """Return per-event entries in sequence/timestamp order, or an empty list.

    After migration, read ``audit_log_legacy``; unified rows and addenda are
    excluded. Timestamps are UTC datetimes. Schema initialization may migrate
    legacy data; database errors propagate.
    """
    ensure_schema()
    from datetime import timezone as _tz

    def _utc(dt) -> datetime:
        # SQLite stores DateTime(timezone=True) as NAIVE UTC — re-attach the
        # UTC tz so isoformat() round-trips exactly (the v2 hash covers the
        # timestamp — A-4 — so it must match what was hashed at write time).
        if dt is None:
            return datetime.now(timezone.utc)
        return dt.replace(tzinfo=_tz.utc) if dt.tzinfo is None else dt.astimezone(_tz.utc)

    async with async_session() as session:
        Model = await event_log_model(session)
        result = await session.execute(
            select(Model)
            .where(Model.doc_id == doc_id)
            .order_by(Model.seq, Model.timestamp)
        )
        records = result.scalars().all()
        return [
            {
                "entry_id": r.entry_id,
                "matter_id": r.matter_id,
                "event": r.event,
                "actor": r.actor,
                "detail": r.detail,
                "prev_hash": r.prev_hash,
                "entry_hash": r.entry_hash,
                "seq": r.seq,
                "timestamp": _utc(r.timestamp),
            }
            for r in records
        ]


async def get_latest_audit_hash(doc_id: str) -> str:
    """Return the per-event chain's latest hash, or ``""`` when no entry exists.

    After migration, read the legacy chain rather than the unified row.
    """
    ensure_schema()
    async with async_session() as session:
        Model = await event_log_model(session)
        result = await session.execute(
            select(Model.entry_hash)
            .where(Model.doc_id == doc_id)
            .order_by(desc(Model.seq), desc(Model.timestamp))
            .limit(1)
        )
        row = result.first()
        return row[0] if row else ""


async def list_audit_doc_ids() -> list[str]:
    """Return sorted nonempty doc ids with per-event entries (legacy after migration)."""
    ensure_schema()
    from sqlalchemy import distinct

    async with async_session() as session:
        Model = await event_log_model(session)
        rows = await session.execute(select(distinct(Model.doc_id)))
        return sorted(r[0] for r in rows.all() if r[0])

async def analyze_audit_db(
    *,
    verify_chains: bool = True,
    event_limit: int = 20,
) -> dict:
    """Summarize per-event audit history (legacy history after migration).

    Returns counts by event/actor, per-doc chain lengths, optional hash-chain
    verification results, and the most recent events. ``verify_chains=False``
    skips chain checks and lengths; ``event_limit`` is clamped to at least 1.
    Unified rows and addenda are excluded. Schema initialization may migrate
    legacy data; database errors propagate.
    """
    from collections import Counter
    from datetime import timezone as _tz

    from schemas.audit import AuditLogEntry, verify_chain
    from sqlalchemy import func

    ensure_schema()

    def _utc(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_tz.utc)

    async with async_session() as session:
        Model = await event_log_model(session)
        total = (await session.execute(select(func.count()).select_from(Model))).scalar() or 0
        by_event_rows = await session.execute(
            select(Model.event, func.count())
            .group_by(Model.event)
            .order_by(func.count().desc())
        )
        by_actor_rows = await session.execute(
            select(Model.actor, func.count())
            .group_by(Model.actor)
            .order_by(func.count().desc())
        )
        doc_count = (
            await session.execute(select(func.count(func.distinct(Model.doc_id))))
        ).scalar() or 0
        matter_count = (
            await session.execute(select(func.count(func.distinct(Model.matter_id))))
        ).scalar() or 0
        recent = await session.execute(
            select(Model)
            .order_by(desc(Model.timestamp), desc(Model.seq))
            .limit(max(1, event_limit))
        )
        recent_rows = list(recent.scalars().all())

    by_event = {e: int(c) for e, c in by_event_rows.all()}
    by_actor = {a: int(c) for a, c in by_actor_rows.all()}

    chain_report: list[dict] = []
    broken = 0
    if verify_chains and total:
        for doc_id in await list_audit_doc_ids():
            records = await get_audit_chain(doc_id)
            entries = [
                AuditLogEntry(
                    entry_id=r["entry_id"],
                    doc_id=doc_id,
                    matter_id=r.get("matter_id") or "",
                    event=r["event"],
                    actor=r["actor"],
                    detail=r["detail"],
                    prev_hash=r["prev_hash"],
                    entry_hash=r["entry_hash"],
                    timestamp=r["timestamp"],
                )
                for r in records
            ]
            ok = verify_chain(entries) if entries else True
            if not ok:
                broken += 1
            chain_report.append(
                {
                    "doc_id": doc_id,
                    "entries": len(records),
                    "ok": ok,
                    "matter_id": records[0].get("matter_id") if records else None,
                }
            )

    review_events = Counter()
    for key in (
        "routed_to_review",
        "review_approved",
        "review_rejected",
        "review_recorded",
        "review_requeued",
        "review_completed",
    ):
        if key in by_event:
            review_events[key] = by_event[key]

    return {
        "total_entries": int(total),
        "distinct_documents": int(doc_count),
        "distinct_matters": int(matter_count),
        "by_event": by_event,
        "by_actor": by_actor,
        "review_events": dict(review_events),
        "chains_checked": len(chain_report) if verify_chains else 0,
        "chains_broken": broken if verify_chains else None,
        "chains": chain_report if verify_chains else [],
        "recent_events": [
            {
                "entry_id": r.entry_id,
                "doc_id": r.doc_id,
                "matter_id": r.matter_id,
                "event": r.event,
                "actor": r.actor,
                "seq": r.seq,
                "timestamp": (_utc(r.timestamp).isoformat() if _utc(r.timestamp) else None),
            }
            for r in recent_rows
        ],
    }
