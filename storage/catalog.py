import structlog
from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime, JSON, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, async_session, ensure_schema

logger = structlog.get_logger(__name__)


class MatterRecord(Base):
    __tablename__ = "matters"

    matter_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    client_name: Mapped[str] = mapped_column(String(256), default="")
    practice_area: Mapped[str] = mapped_column(String(128), default="transactional")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class DocumentRecord(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    matter_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), default="inbox")
    doc_type: Mapped[str] = mapped_column(String(64), nullable=True)
    contract_subtype: Mapped[str] = mapped_column(String(64), nullable=True)
    classification_confidence: Mapped[float] = mapped_column(Float, nullable=True)
    extraction_confidence: Mapped[float] = mapped_column(Float, nullable=True)
    extracted_data: Mapped[dict] = mapped_column(JSON, nullable=True)
    escalation_reason: Mapped[str] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=True)
    scores: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


async def write_matter_record(matter_data) -> MatterRecord:
    ensure_schema()
    async with async_session() as session:
        existing = await session.get(MatterRecord, matter_data.matter_id)
        if existing:
            existing.name = matter_data.name or existing.name
            existing.client_name = matter_data.client_name or existing.client_name
            existing.updated_at = datetime.now(timezone.utc)
        else:
            existing = MatterRecord(
                matter_id=matter_data.matter_id,
                name=matter_data.name,
                client_name=matter_data.client_name,
                practice_area=matter_data.practice_area,
                opened_at=matter_data.opened_at,
            )
            session.add(existing)
        await session.commit()
        return existing


async def write_document_record(doc_data: dict) -> DocumentRecord:
    ensure_schema()
    async with async_session() as session:
        existing = await session.get(DocumentRecord, doc_data["doc_id"])
        if existing:
            existing.stage = doc_data.get("stage", existing.stage)
            existing.doc_type = doc_data.get("doc_type", existing.doc_type)
            existing.contract_subtype = doc_data.get("contract_subtype", existing.contract_subtype)
            existing.classification_confidence = doc_data.get("classification_confidence", existing.classification_confidence)
            existing.extraction_confidence = doc_data.get("extraction_confidence", existing.extraction_confidence)
            existing.extracted_data = doc_data.get("extracted_data", existing.extracted_data)
            existing.escalation_reason = doc_data.get("escalation_reason", existing.escalation_reason)
            existing.trace_id = doc_data.get("trace_id", existing.trace_id)
            existing.updated_at = datetime.now(timezone.utc)
        else:
            record = DocumentRecord(
                doc_id=doc_data["doc_id"],
                matter_id=doc_data["matter_id"],
                original_filename=doc_data["original_filename"],
                stage=doc_data.get("stage", "inbox"),
                doc_type=doc_data.get("doc_type"),
                contract_subtype=doc_data.get("contract_subtype"),
                classification_confidence=doc_data.get("classification_confidence"),
                extraction_confidence=doc_data.get("extraction_confidence"),
                extracted_data=doc_data.get("extracted_data"),
                escalation_reason=doc_data.get("escalation_reason"),
                trace_id=doc_data.get("trace_id"),
            )
            session.add(record)
        await session.commit()
        return existing if existing else record


async def get_document(doc_id: str) -> DocumentRecord | None:
    ensure_schema()
    async with async_session() as session:
        return await session.get(DocumentRecord, doc_id)


async def list_documents(limit: int | None = None) -> list[DocumentRecord]:
    ensure_schema()
    async with async_session() as session:
        query = select(DocumentRecord).order_by(DocumentRecord.updated_at.desc())
        if limit:
            query = query.limit(limit)
        result = await session.execute(query)
        return list(result.scalars().all())


async def update_document_scores(doc_id: str, scores: dict) -> None:
    ensure_schema()
    async with async_session() as session:
        record = await session.get(DocumentRecord, doc_id)
        if record is None:
            logger.warning("scores_update_missing_doc", doc_id=doc_id)
            return
        record.scores = scores
        record.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def get_matter_documents(matter_id: str) -> list[DocumentRecord]:
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(DocumentRecord).where(DocumentRecord.matter_id == matter_id)
        )
        return list(result.scalars().all())


async def get_stuck_documents(stale_minutes: int = 15) -> list[DocumentRecord]:
    ensure_schema()
    cutoff = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = cutoff - timedelta(minutes=stale_minutes)
    async with async_session() as session:
        result = await session.execute(
            select(DocumentRecord).where(
                # Any non-terminal stage can be "stuck": a process kill in the
                # catalog_write → archive window leaves stage=classified with
                # the file still in processing/. Review docs are NOT stuck —
                # they legitimately await a human decision (counted separately
                # as review_queue).
                DocumentRecord.stage.in_(["processing", "inbox", "classified"]),
                DocumentRecord.updated_at < cutoff,
            )
        )
        return list(result.scalars().all())


async def get_documents_by_stage(stage: str) -> list[DocumentRecord]:
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(DocumentRecord).where(DocumentRecord.stage == stage)
        )
        return list(result.scalars().all())


async def get_error_rate_by_doc_type() -> dict[str, dict]:
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(DocumentRecord.doc_type, DocumentRecord.stage)
        )
        rows = result.all()
        stats: dict[str, dict] = {}
        for doc_type, stage in rows:
            if not doc_type:
                # Documents with no classification (unknown-type review,
                # ingest-crash aborts) are not a doc-type bucket; skip them so
                # the response JSON never carries a None key.
                continue
            if doc_type not in stats:
                stats[doc_type] = {"total": 0, "failed": 0, "review": 0}
            stats[doc_type]["total"] += 1
            if stage == "failed":
                stats[doc_type]["failed"] += 1
            elif stage == "review":
                stats[doc_type]["review"] += 1
        return stats
