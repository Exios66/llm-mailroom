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
