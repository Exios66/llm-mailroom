import structlog
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, Text, Integer, ForeignKey, select, desc
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, async_session, ensure_schema

logger = structlog.get_logger(__name__)


class AuditLogRecord(Base):
    __tablename__ = "audit_log"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(128),
        # A-5: an audit entry must belong to a catalog document (ON DELETE
        # RESTRICT — documents are never deleted, so the FK only prevents
        # orphaned chains).
        ForeignKey("documents.doc_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    matter_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(256), default="")
    entry_hash: Mapped[str] = mapped_column(String(256), default="")
    # A-3: monotonic per-doc sequence — tie-breaks appends that land in the
    # same timestamp bucket so the chain order is deterministic even when two
    # entries are written 59 µs apart (the observed chain-break scenario).
    seq: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def write_audit_entry(entry) -> AuditLogRecord:
    ensure_schema()
    from schemas.audit import AuditLogEntry
    async with async_session() as session:
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
        # doc, read inside the same transaction as the append (single writer
        # under WAL, busy_timeout 5 s), so concurrent appends cannot interleave.
        from sqlalchemy import func

        max_seq = await session.execute(
            select(func.coalesce(func.max(AuditLogRecord.seq), 0))
            .where(AuditLogRecord.doc_id == entry.doc_id)
        )
        next_seq = (max_seq.scalar() or 0) + 1
        record = AuditLogRecord(
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
        result = await session.execute(
            select(AuditLogRecord)
            .where(AuditLogRecord.doc_id == doc_id)
            .order_by(AuditLogRecord.seq, AuditLogRecord.timestamp)
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
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(AuditLogRecord.entry_hash)
            .where(AuditLogRecord.doc_id == doc_id)
            .order_by(desc(AuditLogRecord.seq), desc(AuditLogRecord.timestamp))
            .limit(1)
        )
        row = result.first()
        return row[0] if row else ""


async def list_audit_doc_ids() -> list[str]:
    """Every doc_id that has at least one audit entry (ordered)."""
    ensure_schema()
    from sqlalchemy import distinct

    async with async_session() as session:
        rows = await session.execute(select(distinct(AuditLogRecord.doc_id)))
        return sorted(r[0] for r in rows.all() if r[0])

async def analyze_audit_db(
    *,
    verify_chains: bool = True,
    event_limit: int = 20,
) -> dict:
    """Parse the full local audit DB into summary stats for operators.

    Returns counts by event/actor, per-doc chain lengths, optional hash-chain
    verification results, and the most recent events. Read-only.
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
        total = (await session.execute(select(func.count()).select_from(AuditLogRecord))).scalar() or 0
        by_event_rows = await session.execute(
            select(AuditLogRecord.event, func.count())
            .group_by(AuditLogRecord.event)
            .order_by(func.count().desc())
        )
        by_actor_rows = await session.execute(
            select(AuditLogRecord.actor, func.count())
            .group_by(AuditLogRecord.actor)
            .order_by(func.count().desc())
        )
        doc_count = (
            await session.execute(select(func.count(func.distinct(AuditLogRecord.doc_id))))
        ).scalar() or 0
        matter_count = (
            await session.execute(select(func.count(func.distinct(AuditLogRecord.matter_id))))
        ).scalar() or 0
        recent = await session.execute(
            select(AuditLogRecord)
            .order_by(desc(AuditLogRecord.timestamp), desc(AuditLogRecord.seq))
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
