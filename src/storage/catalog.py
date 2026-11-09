import structlog
from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime, JSON, Text, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, async_session, ensure_schema, run_write_with_retry

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
    doc_subclass: Mapped[str] = mapped_column(String(64), nullable=True)
    classification_confidence: Mapped[float] = mapped_column(Float, nullable=True)
    extraction_confidence: Mapped[float] = mapped_column(Float, nullable=True)
    extracted_data: Mapped[dict] = mapped_column(JSON, nullable=True)
    escalation_reason: Mapped[str] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=True)
    scores: Mapped[dict] = mapped_column(JSON, nullable=True)
    # A-10: end-to-end provenance — doc → run → trace → prompt → model → cost.
    # Persisted post-invoke so the chain is reconstructable even with tracing
    # off (trace_id was empty 60/60 in the live DB audit).
    run_id: Mapped[str] = mapped_column(String(128), nullable=True)
    model: Mapped[str] = mapped_column(String(256), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=True)
    latency_s: Mapped[float] = mapped_column(Float, nullable=True)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


def _insert_for(session):
    """Dialect-specific ``insert`` that supports ON CONFLICT (None if neither)."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        return None
    return insert


# Document columns an update overwrites when the caller supplies the key
# (an absent key keeps the stored value; an explicit None clears it).
_DOC_UPDATE_KEYS = (
    "stage",
    "doc_type",
    "contract_subtype",
    "doc_subclass",
    "classification_confidence",
    "extraction_confidence",
    "extracted_data",
    "escalation_reason",
    "trace_id",
    "file_sha256",
)


async def write_matter_record(matter_data) -> MatterRecord:
    """Insert or update a matter as ONE atomic upsert.

    The old get-then-insert raced under concurrent pipelines (UNIQUE
    ``matters.matter_id``). Blank ``name`` / ``client_name`` keep the stored
    values; ``practice_area`` / ``opened_at`` are set on first insert only.
    """
    ensure_schema()

    async def _op() -> MatterRecord:
        async with async_session() as session:
            insert = _insert_for(session)
            now = datetime.now(timezone.utc)
            if insert is None:  # unknown dialect: legacy get-then-write
                existing = await session.get(MatterRecord, matter_data.matter_id)
                if existing:
                    existing.name = matter_data.name or existing.name
                    existing.client_name = matter_data.client_name or existing.client_name
                    existing.updated_at = now
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
            stmt = insert(MatterRecord).values(
                matter_id=matter_data.matter_id,
                name=matter_data.name,
                client_name=matter_data.client_name,
                practice_area=matter_data.practice_area,
                opened_at=matter_data.opened_at,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[MatterRecord.matter_id],
                set_={
                    "name": func.coalesce(func.nullif(stmt.excluded.name, ""), MatterRecord.name),
                    "client_name": func.coalesce(
                        func.nullif(stmt.excluded.client_name, ""), MatterRecord.client_name
                    ),
                    "updated_at": now,
                },
            )
            await session.execute(stmt)
            await session.commit()
            return await session.get(MatterRecord, matter_data.matter_id)

    return await run_write_with_retry(_op)


async def write_document_record(doc_data: dict) -> DocumentRecord:
    """Insert or update a document row as ONE atomic upsert (see
    ``write_matter_record``); concurrent writers of the same ``doc_id`` can no
    longer fail with a UNIQUE violation and drop the stage update."""
    ensure_schema()

    async def _op() -> DocumentRecord:
        async with async_session() as session:
            insert = _insert_for(session)
            now = datetime.now(timezone.utc)
            if insert is None:  # unknown dialect: legacy get-then-write
                existing = await session.get(DocumentRecord, doc_data["doc_id"])
                if existing:
                    for key in _DOC_UPDATE_KEYS:
                        if key in doc_data:
                            setattr(existing, key, doc_data[key])
                    existing.updated_at = now
                else:
                    existing = DocumentRecord(**_doc_insert_values(doc_data))
                    session.add(existing)
                await session.commit()
                return existing
            if "matter_id" not in doc_data or "original_filename" not in doc_data:
                # Update-only caller (e.g. recover_processing passes just
                # doc_id + stage): never inserts, exactly like the old path.
                values = {k: doc_data[k] for k in _DOC_UPDATE_KEYS if k in doc_data}
                result = await session.execute(
                    update(DocumentRecord)
                    .where(DocumentRecord.doc_id == doc_data["doc_id"])
                    .values(**values, updated_at=now)
                )
                if result.rowcount == 0:
                    raise KeyError("matter_id" if "matter_id" not in doc_data else "original_filename")
                await session.commit()
                return await session.get(DocumentRecord, doc_data["doc_id"])
            stmt = insert(DocumentRecord).values(
                **_doc_insert_values(doc_data), created_at=now, updated_at=now
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[DocumentRecord.doc_id],
                set_={**{k: doc_data[k] for k in _DOC_UPDATE_KEYS if k in doc_data}, "updated_at": now},
            )
            await session.execute(stmt)
            await session.commit()
            return await session.get(DocumentRecord, doc_data["doc_id"])

    return await run_write_with_retry(_op)


def _doc_insert_values(doc_data: dict) -> dict:
    return {
        "doc_id": doc_data["doc_id"],
        "matter_id": doc_data["matter_id"],
        "original_filename": doc_data["original_filename"],
        "stage": doc_data.get("stage", "inbox"),
        "doc_type": doc_data.get("doc_type"),
        "contract_subtype": doc_data.get("contract_subtype"),
        "doc_subclass": doc_data.get("doc_subclass"),
        "classification_confidence": doc_data.get("classification_confidence"),
        "extraction_confidence": doc_data.get("extraction_confidence"),
        "extracted_data": doc_data.get("extracted_data"),
        "escalation_reason": doc_data.get("escalation_reason"),
        "trace_id": doc_data.get("trace_id"),
        "file_sha256": doc_data.get("file_sha256"),
    }


async def get_document(doc_id: str) -> DocumentRecord | None:
    ensure_schema()
    async with async_session() as session:
        return await session.get(DocumentRecord, doc_id)


async def lookup_document(
    *,
    doc_id: str | None = None,
    trace_id: str | None = None,
    filename: str | None = None,
) -> DocumentRecord | None:
    """Resolve a catalog row by doc_id, then trace_id, then original filename.

    Preferential order matches The-Mailroom review proxy (PR #18): an explicit
    ``doc_id`` wins; otherwise the newest match for ``trace_id`` / filename.
    """
    ensure_schema()
    async with async_session() as session:
        if doc_id:
            row = await session.get(DocumentRecord, doc_id)
            if row is not None:
                return row
        if trace_id:
            result = await session.execute(
                select(DocumentRecord)
                .where(DocumentRecord.trace_id == trace_id)
                .order_by(DocumentRecord.updated_at.desc())
                .limit(1)
            )
            row = result.scalars().first()
            if row is not None:
                return row
        if filename:
            result = await session.execute(
                select(DocumentRecord)
                .where(DocumentRecord.original_filename == filename)
                .order_by(DocumentRecord.updated_at.desc())
                .limit(1)
            )
            return result.scalars().first()
    return None


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


async def update_document_provenance(
    doc_id: str,
    *,
    run_id: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    cost_usd: float | None = None,
    latency_s: float | None = None,
    file_sha256: str | None = None,
) -> None:
    """A-10: persist end-to-end provenance columns (doc → run → model →
    prompt → cost → latency → file hash) on the document row."""
    ensure_schema()
    async with async_session() as session:
        record = await session.get(DocumentRecord, doc_id)
        if record is None:
            logger.warning("provenance_update_missing_doc", doc_id=doc_id)
            return
        if run_id is not None:
            record.run_id = run_id
        if model is not None:
            record.model = model
        if prompt_version is not None:
            record.prompt_version = prompt_version
        if cost_usd is not None:
            record.cost_usd = cost_usd
        if latency_s is not None:
            record.latency_s = latency_s
        if file_sha256 is not None:
            record.file_sha256 = file_sha256
        record.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def get_matter_documents(matter_id: str) -> list[DocumentRecord]:
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(DocumentRecord).where(DocumentRecord.matter_id == matter_id)
        )
        return list(result.scalars().all())


async def touch_document_heartbeat(doc_id: str) -> None:
    """L-5: bump `updated_at` for a document row (progress heartbeat).

    Called at every graph-node boundary for an in-flight document, so stuck
    detection (which keys on updated_at) never flags a document that is
    actively processing through a long retry storm or transcription."""
    try:
        async with async_session() as session:
            record = await session.get(DocumentRecord, doc_id)
            if record is None:
                return
            record.updated_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception:
        pass  # best-effort heartbeat


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


async def count_first_pass_throughput() -> dict[str, int | float | None]:
    """Catalog counts for production STP (no ground truth).

    ``first_pass`` is documents whose persisted scores mark ``success_rate``
    as 1 — archived in one hop without retry/review/arbiter/boss. Missing
    scores (runs from before this metric) are not counted as first-pass.
    """
    archived = await get_documents_by_stage("archived")
    first_pass = 0
    for record in archived:
        value = (record.scores or {}).get("success_rate")
        try:
            if float(value) >= 0.5:
                first_pass += 1
        except (TypeError, ValueError):
            continue
    n_archived = len(archived)
    rate = round(first_pass / n_archived, 4) if n_archived else None
    return {
        "archived": n_archived,
        "first_pass": first_pass,
        "first_pass_rate": rate,
    }


async def get_recent_documents(limit: int = 20) -> list[DocumentRecord]:
    """Most recently updated document records (for the /queue recent view)."""
    ensure_schema()
    async with async_session() as session:
        result = await session.execute(
            select(DocumentRecord).order_by(DocumentRecord.updated_at.desc()).limit(limit)
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


async def window_stats(since: datetime, run_id: str | None = None) -> dict:
    """Operator-pager window counts: ``received`` (created since), terminal
    ``archived``/``review``/``failed`` (current stage, updated since), plus the
    window's cost, median latency and first-pass rate (None when empty)."""
    ensure_schema()
    q = select(DocumentRecord).where(
        (DocumentRecord.created_at >= since) | (DocumentRecord.updated_at >= since)
    )
    if run_id:
        q = q.where(DocumentRecord.run_id == run_id)
    async with async_session() as session:
        rows = list((await session.execute(q)).scalars().all())
    since_naive = since.replace(tzinfo=None)

    def after(ts):
        return ts is not None and ts.replace(tzinfo=None) >= since_naive

    done = [r for r in rows if after(r.updated_at)]
    by = {s: [r for r in done if r.stage == s] for s in ("archived", "review", "failed")}
    lats = sorted(r.latency_s for r in done if r.latency_s is not None)
    scored = [r for r in by["archived"] if (r.scores or {}).get("success_rate") is not None]
    mid = len(lats) // 2
    return {
        "received": sum(1 for r in rows if after(r.created_at)),
        **{s: len(v) for s, v in by.items()},
        "cost_usd": round(sum(r.cost_usd or 0.0 for r in done), 4),
        "p50_latency_s": (lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2) if lats else None,
        "first_pass_rate": (sum(1 for r in scored if float(r.scores["success_rate"]) >= 0.5) / len(scored))
        if scored else None,
    }


async def recent_failures(since: datetime, limit: int = 3) -> list[dict]:
    """Newest failed documents updated since ``since``, for the pager's
    ``Latest:`` line: filename, stage, a ≤60-char reason and the update time."""
    ensure_schema()
    async with async_session() as session:
        rows = (await session.execute(
            select(DocumentRecord).where(DocumentRecord.stage == "failed", DocumentRecord.updated_at >= since)
            .order_by(DocumentRecord.updated_at.desc()).limit(limit)
        )).scalars().all()
    out = []
    for r in rows:
        reason = r.escalation_reason or "unknown error"
        try:
            from pipeline.gmail_intake import friendly_reason

            reason = (friendly_reason(reason) or (reason,))[0]
        except Exception:
            pass
        ts = r.updated_at.replace(tzinfo=r.updated_at.tzinfo or timezone.utc).timestamp()
        out.append({"filename": r.original_filename, "stage": r.stage, "reason": reason[:60], "at": ts})
    return out
