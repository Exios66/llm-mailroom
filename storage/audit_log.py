import structlog
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, Text, select, desc
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, async_session, ensure_schema

logger = structlog.get_logger(__name__)


class AuditLogRecord(Base):
    __tablename__ = "audit_log"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    matter_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(256), default="")
    entry_hash: Mapped[str] = mapped_column(String(256), default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def write_audit_entry(entry) -> AuditLogRecord:
    ensure_schema()
    from schemas.audit import AuditLogEntry
    async with async_session() as session:
        record = AuditLogRecord(
            entry_id=entry.entry_id,
            doc_id=entry.doc_id,
            matter_id=entry.matter_id,
            event=entry.event,
            actor=entry.actor,
            detail=entry.detail,
            prev_hash=entry.prev_hash,
            entry_hash=entry.entry_hash,
            timestamp=entry.timestamp,
        )
        session.add(record)
        await session.commit()
        logger.info("audit_entry_written", entry_id=entry.entry_id, event_name=entry.event)
        return record


async def get_audit_chain(doc_id: str) -> list[dict]:
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(AuditLogRecord)
            .where(AuditLogRecord.doc_id == doc_id)
            .order_by(AuditLogRecord.timestamp)
        )
        records = result.scalars().all()
        return [
            {
                "entry_id": r.entry_id,
                "event": r.event,
                "actor": r.actor,
                "detail": r.detail,
                "prev_hash": r.prev_hash,
                "entry_hash": r.entry_hash,
                "timestamp": r.timestamp.isoformat(),
            }
            for r in records
        ]


async def get_latest_audit_hash(doc_id: str) -> str:
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(AuditLogRecord.entry_hash)
            .where(AuditLogRecord.doc_id == doc_id)
            .order_by(desc(AuditLogRecord.timestamp))
            .limit(1)
        )
        row = result.first()
        return row[0] if row else ""
