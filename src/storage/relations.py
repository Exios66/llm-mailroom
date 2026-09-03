"""Relations storage — the auditable relations ledger (HUB-040).

Three tables on the shared engine (same SQLite/Postgres duality as the rest
of storage; created idempotently by ``db.ensure_schema`` via
``_ensure_models_imported``):

- ``relation_edges`` — the graph itself: typed, scored, evidenced edges
  between archived documents (and, via bridge edges, matters). Upsert
  semantics keyed on (source, target, type) so re-scans refresh scores
  instead of duplicating.
- ``relation_log`` — the OWN hash-chained ledger required by the human
  directive: every scan, every recorded edge, every graph render is an
  entry in a single global chain (scope ``__relations__``). Reuses
  ``schemas/audit.py`` hash primitives + ``verify_chain`` verbatim — the
  chain is tamper-evident with the same law as the per-document audit.
- ``relation_embeddings`` — the efficiency core: one embedding per document
  (model-tagged), computed once and reused by every later scan.
- ``relation_scan_state`` — a tiny KV store for the scanner watermark and
  cadence bookkeeping (incremental scans, never full recomputation).

Async (same async_session pattern as audit_log); sync daemon-thread callers
wrap with ``asyncio.run`` in ``pipeline/relations.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, String, Text, UniqueConstraint
from sqlalchemy import JSON as SAJSON
from sqlalchemy import LargeBinary
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
from sqlalchemy.orm import Mapped, mapped_column

from storage.db import Base, async_session, ensure_schema

try:  # pragma: no cover - JSON variant is engine-dependent
    JSONType = SAJSON().with_variant(SQLITE_JSON(), "sqlite")
except Exception:  # pragma: no cover
    JSONType = SAJSON

_DT = DateTime(timezone=True)
"""Shared timezone-aware column type (house pattern — catalog.py)."""


def _now():
    return datetime.now(timezone.utc)



RELATIONS_CHAIN_SCOPE = "__relations__"

# Closed vocabulary of relation types (clamped everywhere — agents and the
# deterministic layer alike never invent types outside this set).
RELATION_TYPES = (
    "same_matter",
    "topic_overlap",
    "semantic_similarity",
    "party_overlap",
    "temporal_proximity",
    "llm_asserted",
)

RELATION_EVENTS = (
    "relations_scan_started",
    "relation_recorded",
    "relations_scan_completed",
    "relations_graph_rendered",
    "relation_revoked",
)


class RelationEdgeRecord(Base):
    __tablename__ = "relation_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_doc_id", "target_doc_id", "relation_type", name="uq_relation_edge"
        ),
        Index("ix_relation_edges_source", "source_doc_id"),
        Index("ix_relation_edges_target", "target_doc_id"),
        Index("ix_relation_edges_type_score", "relation_type", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    method: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    source_matter_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_matter_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    scanner_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _DT, nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(_DT, nullable=False, default=_now)


class RelationLogRecord(Base):
    __tablename__ = "relation_log"
    __table_args__ = (
        Index("ix_relation_log_timestamp", "timestamp"),
        UniqueConstraint("entry_id", name="uq_relation_log_entry"),
    )

    # Autoincrement id gives the write order a stable authority (sub-millisecond
    # entries share timestamps; the chain's prev-links follow id order).
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default=RELATIONS_CHAIN_SCOPE)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="relations")
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    timestamp: Mapped[datetime] = mapped_column(_DT, nullable=False, default=_now)


class RelationEmbeddingRecord(Base):
    __tablename__ = "relation_embeddings"

    doc_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    dim: Mapped[int] = mapped_column(nullable=False, default=0)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    created_at: Mapped[datetime] = mapped_column(_DT, nullable=False, default=_now)


class RelationScanStateRecord(Base):
    __tablename__ = "relation_scan_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(_DT, nullable=False, default=_now)


async def _latest_ledger_row(session):
    from sqlalchemy import select

    stmt = (
        select(RelationLogRecord)
        .where(RelationLogRecord.scope == RELATIONS_CHAIN_SCOPE)
        .order_by(RelationLogRecord.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_latest_relation_hash() -> str:
    """Latest entry_hash in the global relations chain ('' when empty)."""
    ensure_schema()
    async with async_session() as session:
        row = await _latest_ledger_row(session)
        return str(row.entry_hash) if row else ""


# ``verify_chain`` sorts by (timestamp, entry_id) — sub-millisecond ledger
# writes can share a timestamp, and the uuid entry_id tiebreak would scramble
# prev-link order. Writers therefore enforce STRICTLY increasing timestamps
# (a collision bumps +1µs past the previous entry), making timestamp order
# identical to id (write) order, so the stock verifier stays authoritative.
def _monotonic_timestamp(prev_ts: datetime | None) -> datetime:
    from datetime import timedelta, timezone as _tz

    now = datetime.now(timezone.utc)
    if prev_ts is None:
        return now
    if prev_ts.tzinfo is None:  # SQLite drops tzinfo — stored values are UTC
        prev_ts = prev_ts.replace(tzinfo=_tz.utc)
    if now <= prev_ts:
        return prev_ts + timedelta(microseconds=1)
    return now


def _utc(dt: datetime) -> datetime:
    """Naive readback → aware UTC (the write path always stores UTC)."""
    from datetime import timezone as _tz

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_tz.utc)


async def write_relation_log_entry(
    event: str, detail: dict, actor: str = "relations"
) -> dict:
    """Append one hash-chained entry to the relations ledger. Never raises to
    callers (relations must never break the document path) — returns the
    entry dict, or {} on failure (logged by the caller's fail-soft)."""
    from schemas.audit import build_audit_entry

    ensure_schema()
    async with async_session() as session:
        prev = await _latest_ledger_row(session)
        prev_hash = str(prev.entry_hash) if prev else ""
        ts = _monotonic_timestamp(prev.timestamp if prev else None)
        entry = build_audit_entry(
            RELATIONS_CHAIN_SCOPE,
            "relations",
            event,
            actor,
            detail,
            prev_hash=prev_hash,
        )
        # Re-stamp with the monotonic timestamp BEFORE hashing so the stored
        # hash covers exactly the stored time.
        entry.timestamp = ts
        entry.entry_hash = entry.entry_hash  # recomputed below
        from schemas.audit import compute_audit_hash, HASH_VERSION  # noqa: F401

        entry.entry_hash = compute_audit_hash(
            entry.prev_hash,
            entry.doc_id,
            entry.entry_id,
            entry.event,
            entry.detail,
            matter_id=entry.matter_id,
            actor=entry.actor,
            timestamp=ts,
        )
        record = RelationLogRecord(
            entry_id=entry.entry_id,
            scope=RELATIONS_CHAIN_SCOPE,
            event=entry.event,
            actor=entry.actor,
            detail=entry.detail,
            prev_hash=entry.prev_hash,
            entry_hash=entry.entry_hash,
            timestamp=ts,
        )
        session.add(record)
        await session.commit()
        return {
            "entry_id": entry.entry_id,
            "event": entry.event,
            "entry_hash": entry.entry_hash,
            "prev_hash": entry.prev_hash,
        }


async def get_relation_chain() -> list[dict]:
    """The full relations ledger in write order (small by design)."""
    from sqlalchemy import select

    ensure_schema()
    async with async_session() as session:
        rows = (
            (
                await session.execute(
                    select(RelationLogRecord).order_by(RelationLogRecord.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "entry_id": r.entry_id,
                "scope": r.scope,
                "event": r.event,
                "actor": r.actor,
                "detail": r.detail,
                "prev_hash": r.prev_hash,
                "entry_hash": r.entry_hash,
                # Normalize naive SQLite readback to aware UTC (house pattern —
                # audit_log.get_audit_chain's _utc) so verify_chain's
                # recomputation hashes the exact stored time.
                "timestamp": _utc(r.timestamp),
            }
            for r in rows
        ]


def normalize_edge(src: str, dst: str) -> tuple[str, str]:
    """Canonical endpoint order — the same pair filed either direction is one
    edge, never two."""
    return (src, dst) if src <= dst else (dst, src)


async def record_edges(edges: list[dict]) -> dict:
    """Upsert edges (keyed on the canonical unique triple). Returns counts.

    Each edge dict: source_doc_id, target_doc_id, relation_type, score,
    method, source_matter_id?, target_matter_id?, evidence, scanner_run_id?.
    Endpoints are normalized; the canonical type vocabulary is enforced; a
    relation_type outside RELATION_TYPES is refused (audit-integrity law:
    nothing unvalidated ever enters the ledger).
    """
    from sqlalchemy import select

    ensure_schema()
    inserted = updated = refused = 0
    inserted_keys: list[tuple[str, str, str]] = []
    async with async_session() as session:
        for edge in edges:
            rtype = str(edge.get("relation_type") or "")
            if rtype not in RELATION_TYPES:
                refused += 1
                continue
            src, dst = normalize_edge(
                str(edge.get("source_doc_id") or ""), str(edge.get("target_doc_id") or "")
            )
            if not src or not dst or src == dst:
                refused += 1
                continue
            stmt = select(RelationEdgeRecord).where(
                RelationEdgeRecord.source_doc_id == src,
                RelationEdgeRecord.target_doc_id == dst,
                RelationEdgeRecord.relation_type == rtype,
            )
            record = (await session.execute(stmt)).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if record is None:
                session.add(
                    RelationEdgeRecord(
                        source_doc_id=src,
                        target_doc_id=dst,
                        relation_type=rtype,
                        score=float(edge.get("score") or 0.0),
                        method=str(edge.get("method") or "deterministic"),
                        source_matter_id=edge.get("source_matter_id"),
                        target_matter_id=edge.get("target_matter_id"),
                        evidence=edge.get("evidence") or {},
                        scanner_run_id=edge.get("scanner_run_id"),
                        created_at=now,
                        updated_at=now,
                    )
                )
                inserted += 1
                inserted_keys.append((src, dst, rtype))
            else:
                record.score = float(edge.get("score") or record.score)
                record.method = str(edge.get("method") or record.method)
                record.evidence = edge.get("evidence") or record.evidence
                record.scanner_run_id = edge.get("scanner_run_id") or record.scanner_run_id
                record.updated_at = now
                updated += 1
        await session.commit()
    return {"inserted": inserted, "updated": updated, "refused": refused, "inserted_keys": inserted_keys}


async def list_edges(
    *,
    doc_id: str | None = None,
    matter_id: str | None = None,
    relation_type: str | None = None,
    min_score: float = 0.0,
    limit: int = 100,
) -> list[dict]:
    """Edges touching a document or matter (either endpoint), newest score
    first — the read path for context injection and graph projection."""
    from sqlalchemy import or_, select

    ensure_schema()
    async with async_session() as session:
        stmt = select(RelationEdgeRecord)
        if doc_id:
            stmt = stmt.where(
                or_(
                    RelationEdgeRecord.source_doc_id == doc_id,
                    RelationEdgeRecord.target_doc_id == doc_id,
                )
            )
        if matter_id:
            stmt = stmt.where(
                or_(
                    RelationEdgeRecord.source_matter_id == matter_id,
                    RelationEdgeRecord.target_matter_id == matter_id,
                )
            )
        if relation_type:
            stmt = stmt.where(RelationEdgeRecord.relation_type == relation_type)
        stmt = stmt.where(RelationEdgeRecord.score >= min_score)
        stmt = stmt.order_by(RelationEdgeRecord.score.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "source_doc_id": r.source_doc_id,
                "target_doc_id": r.target_doc_id,
                "relation_type": r.relation_type,
                "score": r.score,
                "method": r.method,
                "source_matter_id": r.source_matter_id,
                "target_matter_id": r.target_matter_id,
                "evidence": r.evidence,
                "scanner_run_id": r.scanner_run_id,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]


async def all_edges(limit: int = 100000) -> list[dict]:
    """Every edge (graph projection read path)."""
    from sqlalchemy import select

    ensure_schema()
    async with async_session() as session:
        rows = (
            (await session.execute(select(RelationEdgeRecord).limit(limit))).scalars().all()
        )
        return [
            {
                "source_doc_id": r.source_doc_id,
                "target_doc_id": r.target_doc_id,
                "relation_type": r.relation_type,
                "score": r.score,
                "method": r.method,
                "source_matter_id": r.source_matter_id,
                "target_matter_id": r.target_matter_id,
                "evidence": r.evidence,
            }
            for r in rows
        ]


async def get_embedding(doc_id: str, model: str) -> list[float] | None:
    from sqlalchemy import select

    ensure_schema()
    async with async_session() as session:
        row = (
            await session.execute(
                select(RelationEmbeddingRecord).where(
                    RelationEmbeddingRecord.doc_id == doc_id,
                    RelationEmbeddingRecord.model == model,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        import array

        arr = array.array("f")
        arr.frombytes(row.vector)
        return list(arr)


async def put_embedding(doc_id: str, model: str, vector: list[float]) -> None:
    import array

    ensure_schema()
    payload = array.array("f", vector).tobytes()
    async with async_session() as session:
        row = (
            await session.execute(
                select_clause := _select_embedding(doc_id, model)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                RelationEmbeddingRecord(
                    doc_id=doc_id,
                    model=model,
                    dim=len(vector),
                    vector=payload,
                    created_at=datetime.now(timezone.utc),
                )
            )
        else:
            row.vector = payload
            row.dim = len(vector)
        await session.commit()


def _select_embedding(doc_id: str, model: str):
    from sqlalchemy import select

    return select(RelationEmbeddingRecord).where(
        RelationEmbeddingRecord.doc_id == doc_id,
        RelationEmbeddingRecord.model == model,
    )


async def all_embeddings(model: str, exclude_doc_id: str | None = None) -> dict[str, list[float]]:
    """Every cached embedding for a model (batch compare input)."""
    from sqlalchemy import select

    ensure_schema()
    async with async_session() as session:
        rows = (
            (
                await session.execute(
                    select(RelationEmbeddingRecord).where(
                        RelationEmbeddingRecord.model == model
                    )
                )
            )
            .scalars()
            .all()
        )
    import array

    out: dict[str, list[float]] = {}
    for r in rows:
        if exclude_doc_id and r.doc_id == exclude_doc_id:
            continue
        arr = array.array("f")
        arr.frombytes(r.vector)
        out[r.doc_id] = list(arr)
    return out


async def get_scan_state(key: str) -> str | None:
    from sqlalchemy import select

    ensure_schema()
    async with async_session() as session:
        row = (
            await session.execute(
                select(RelationScanStateRecord).where(RelationScanStateRecord.key == key)
            )
        ).scalar_one_or_none()
        return row.value if row else None


async def set_scan_state(key: str, value: str) -> None:
    ensure_schema()
    async with async_session() as session:
        row = (
            await session.execute(
                select_state := _select_state(key)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                RelationScanStateRecord(
                    key=key, value=value, updated_at=datetime.now(timezone.utc)
                )
            )
        else:
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
        await session.commit()


def _select_state(key: str):
    from sqlalchemy import select

    return select(RelationScanStateRecord).where(RelationScanStateRecord.key == key)


def new_scanner_run_id() -> str:
    return uuid.uuid4().hex[:12]


async def relations_context(
    *, matter_id: str | None = None, doc_id: str | None = None, limit: int = 6
) -> list[dict]:
    """Bounded related-work read for context injection (agents + echo).

    Returns the top-``limit`` edges touching the matter/document, each with
    the counterpart's filename/type resolved from the catalog when available
    — formatted upstream, raw here."""
    edges = await list_edges(doc_id=doc_id, matter_id=matter_id, limit=limit)
    if not edges:
        return []
    from sqlalchemy import or_, select

    from storage.catalog import DocumentRecord

    doc_ids = {e["source_doc_id"] for e in edges} | {e["target_doc_id"] for e in edges}
    meta: dict[str, dict] = {}
    async with async_session() as session:
        rows = (
            (
                await session.execute(
                    select(DocumentRecord).where(or_(*[DocumentRecord.doc_id == d for d in doc_ids]))
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            meta[r.doc_id] = {
                "original_filename": r.original_filename,
                "doc_type": r.doc_type,
                "matter_id": r.matter_id,
            }
    out = []
    for e in edges:
        other = (
            e["target_doc_id"]
            if e["source_doc_id"] == doc_id
            else e["source_doc_id"]
        ) or e["target_doc_id"]
        out.append(
            {
                "relation_type": e["relation_type"],
                "score": e["score"],
                "method": e["method"],
                "other_doc_id": other,
                "other": meta.get(other, {}),
                "evidence": e.get("evidence") or {},
            }
        )
    return out


async def count_edges() -> int:
    from sqlalchemy import func, select

    ensure_schema()
    async with async_session() as session:
        return int((await session.execute(select(func.count()).select_from(RelationEdgeRecord))).scalar() or 0)
