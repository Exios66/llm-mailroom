"""Parquet cold store for finished documents + matching audit chains.

Layout under ``{MAILROOM_BASE_DIR}/warehouse/``::

    documents_YYYY-MM-DD.parquet   # terminal-stage catalog rows
    audit_YYYY-MM-DD.parquet       # audit_log rows for those doc_ids
    manifest.json                  # schema version + export watermark

Join key: ``doc_id``. Routine export runs after archive/failed (best-effort).
Full/incremental backfill: ``scripts/export_warehouse.py``.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

WAREHOUSE_SCHEMA_VERSION = "1"
TERMINAL_STAGES = frozenset({"archived", "failed"})
_MANIFEST_NAME = "manifest.json"


def warehouse_export_enabled() -> bool:
    raw = os.environ.get("MAILROOM_WAREHOUSE_EXPORT", "auto").strip().lower()
    if raw in ("0", "false", "no", "off", "disabled"):
        return False
    if raw in ("1", "true", "yes", "on", "enabled"):
        return True
    # auto: enabled when pyarrow is importable (pulled in by arize-phoenix)
    try:
        import pyarrow  # noqa: F401

        return True
    except ImportError:
        return False


def warehouse_dir() -> Path:
    from pipeline.bins import get_base_dir

    return get_base_dir() / "warehouse"


def manifest_file() -> Path:
    return warehouse_dir() / _MANIFEST_NAME


def daily_documents_path(stamp: date) -> Path:
    return warehouse_dir() / f"documents_{stamp.isoformat()}.parquet"


def daily_audit_path(stamp: date) -> Path:
    return warehouse_dir() / f"audit_{stamp.isoformat()}.parquet"


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def load_warehouse_manifest() -> dict[str, Any]:
    path = manifest_file()
    if not path.is_file():
        return {
            "schema_version": WAREHOUSE_SCHEMA_VERSION,
            "last_export_at": None,
            "last_doc_updated_at": None,
            "daily_files": {},
        }
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("manifest must be an object")
        data.setdefault("schema_version", WAREHOUSE_SCHEMA_VERSION)
        data.setdefault("daily_files", {})
        return data
    except Exception:
        logger.exception("warehouse_manifest_read_failed")
        return {
            "schema_version": WAREHOUSE_SCHEMA_VERSION,
            "last_export_at": None,
            "last_doc_updated_at": None,
            "daily_files": {},
        }


def save_warehouse_manifest(manifest: dict[str, Any]) -> None:
    warehouse_dir().mkdir(parents=True, exist_ok=True)
    manifest["schema_version"] = WAREHOUSE_SCHEMA_VERSION
    manifest_file().write_text(json.dumps(manifest, indent=2, default=str) + "\n")


def document_to_row(record) -> dict[str, Any]:
    """Flatten a ``DocumentRecord`` (or dict) for Parquet."""
    if hasattr(record, "doc_id"):
        data = {
            "doc_id": record.doc_id,
            "matter_id": record.matter_id,
            "original_filename": record.original_filename,
            "stage": record.stage,
            "doc_type": record.doc_type,
            "contract_subtype": record.contract_subtype,
            "doc_subclass": record.doc_subclass,
            "classification_confidence": record.classification_confidence,
            "extraction_confidence": record.extraction_confidence,
            "escalation_reason": record.escalation_reason,
            "trace_id": record.trace_id,
            "run_id": record.run_id,
            "model": record.model,
            "prompt_version": record.prompt_version,
            "cost_usd": record.cost_usd,
            "latency_s": record.latency_s,
            "file_sha256": record.file_sha256,
            "created_at": _utc_iso(record.created_at),
            "updated_at": _utc_iso(record.updated_at),
            "extracted_data_json": json.dumps(record.extracted_data, default=str)
            if record.extracted_data is not None
            else None,
            "scores_json": json.dumps(record.scores, default=str)
            if record.scores is not None
            else None,
        }
    else:
        data = dict(record)
        for key in ("extracted_data", "scores"):
            if key in data and not isinstance(data.get(f"{key}_json"), str):
                val = data.pop(key, None)
                data[f"{key}_json"] = json.dumps(val, default=str) if val is not None else None
        for ts_key in ("created_at", "updated_at"):
            if ts_key in data and hasattr(data[ts_key], "isoformat"):
                data[ts_key] = _utc_iso(data[ts_key])
    data["exported_at"] = datetime.now(timezone.utc).isoformat()
    return data


def audit_to_row(entry: dict[str, Any]) -> dict[str, Any]:
    ts = entry.get("timestamp")
    if hasattr(ts, "isoformat"):
        ts = _utc_iso(ts)
    return {
        "entry_id": entry.get("entry_id"),
        "doc_id": entry.get("doc_id"),
        "matter_id": entry.get("matter_id"),
        "event": entry.get("event"),
        "actor": entry.get("actor"),
        "detail_json": json.dumps(entry.get("detail") or {}, default=str),
        "prev_hash": entry.get("prev_hash"),
        "entry_hash": entry.get("entry_hash"),
        "seq": entry.get("seq"),
        "timestamp": ts,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "pyarrow is required for warehouse export (install mailroom with "
            "arize-phoenix or pip install pyarrow)"
        ) from exc
    return pa, pq


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    _, pq = _require_pyarrow()
    return pq.read_table(path).to_pylist()


def _merge_rows(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    merged = {str(r[key]): r for r in existing if r.get(key) is not None}
    for row in incoming:
        k = row.get(key)
        if k is not None:
            merged[str(k)] = row
    return list(merged.values())


def write_parquet_rows(path: Path, rows: list[dict[str, Any]], *, merge_key: str) -> int:
    """Upsert ``rows`` into ``path`` (create or merge by ``merge_key``)."""
    if not rows:
        return 0
    pa, pq = _require_pyarrow()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _merge_rows(_read_parquet_rows(path), rows, merge_key)
    table = pa.Table.from_pylist(merged)
    pq.write_table(table, path, compression="snappy")
    return len(merged)


async def fetch_terminal_documents(
    *,
    doc_ids: list[str] | None = None,
    since: datetime | None = None,
) -> list:
    from sqlalchemy import select

    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    ensure_schema()
    async with async_session() as session:
        if doc_ids:
            result = await session.execute(
                select(DocumentRecord).where(
                    DocumentRecord.doc_id.in_(doc_ids),
                    DocumentRecord.stage.in_(tuple(TERMINAL_STAGES)),
                )
            )
            return list(result.scalars().all())
        query = select(DocumentRecord).where(
            DocumentRecord.stage.in_(tuple(TERMINAL_STAGES))
        )
        if since is not None:
            query = query.where(DocumentRecord.updated_at > since)
        query = query.order_by(DocumentRecord.updated_at)
        result = await session.execute(query)
        return list(result.scalars().all())


async def fetch_audit_rows_for_doc_ids(doc_ids: list[str]) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    from sqlalchemy import select

    from storage.audit_log import AuditLogRecord, get_audit_chain
    from storage.db import async_session, ensure_schema

    ensure_schema()
    rows: list[dict[str, Any]] = []
    # Per-doc chain preserves seq order; batch read for large sets.
    if len(doc_ids) <= 50:
        for doc_id in doc_ids:
            rows.extend(await get_audit_chain(doc_id))
        return rows
    async with async_session() as session:
        result = await session.execute(
            select(AuditLogRecord)
            .where(AuditLogRecord.doc_id.in_(doc_ids))
            .order_by(AuditLogRecord.doc_id, AuditLogRecord.seq, AuditLogRecord.timestamp)
        )
        for rec in result.scalars().all():
            rows.append(
                {
                    "entry_id": rec.entry_id,
                    "doc_id": rec.doc_id,
                    "matter_id": rec.matter_id,
                    "event": rec.event,
                    "actor": rec.actor,
                    "detail": rec.detail,
                    "prev_hash": rec.prev_hash,
                    "entry_hash": rec.entry_hash,
                    "seq": rec.seq,
                    "timestamp": rec.timestamp,
                }
            )
    return rows


async def export_to_warehouse(
    *,
    doc_ids: list[str] | None = None,
    stamp: date | None = None,
    since: datetime | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Export terminal documents + audit rows into daily Parquet files."""
    if not warehouse_export_enabled():
        return {"status": "skipped", "reason": "warehouse export disabled or pyarrow missing"}

    stamp = stamp or datetime.now(timezone.utc).date()
    wh_manifest = load_warehouse_manifest()

    if full:
        since = None
    elif since is None and not doc_ids:
        raw = wh_manifest.get("last_doc_updated_at")
        if raw:
            try:
                since = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                since = None

    records = await fetch_terminal_documents(doc_ids=doc_ids, since=since)
    if not records:
        return {
            "status": "ok",
            "exported_documents": 0,
            "exported_audit_entries": 0,
            "stamp": stamp.isoformat(),
        }

    doc_rows = [document_to_row(r) for r in records]
    ids = [r.doc_id for r in records]
    audit_raw = await fetch_audit_rows_for_doc_ids(ids)
    audit_rows = [audit_to_row(e) for e in audit_raw]

    doc_path = daily_documents_path(stamp)
    audit_path = daily_audit_path(stamp)
    n_docs = write_parquet_rows(doc_path, doc_rows, merge_key="doc_id")
    n_audit = write_parquet_rows(audit_path, audit_rows, merge_key="entry_id")

    latest_updated = max(
        (r.updated_at for r in records if r.updated_at is not None),
        default=None,
    )
    now = datetime.now(timezone.utc).isoformat()
    wh_manifest["last_export_at"] = now
    if latest_updated is not None:
        wh_manifest["last_doc_updated_at"] = _utc_iso(latest_updated)
    daily = wh_manifest.setdefault("daily_files", {})
    daily[stamp.isoformat()] = {
        "documents": doc_path.name,
        "audit": audit_path.name,
        "document_count": n_docs,
        "audit_count": n_audit,
        "last_batch_at": now,
        "batch_documents": len(doc_rows),
        "batch_audit_entries": len(audit_rows),
    }
    save_warehouse_manifest(wh_manifest)

    logger.info(
        "warehouse_export_complete",
        stamp=stamp.isoformat(),
        documents=n_docs,
        audit_entries=n_audit,
        batch_docs=len(doc_rows),
    )
    return {
        "status": "ok",
        "stamp": stamp.isoformat(),
        "documents_path": str(doc_path),
        "audit_path": str(audit_path),
        "exported_documents": len(doc_rows),
        "exported_audit_entries": len(audit_rows),
        "total_documents_in_file": n_docs,
        "total_audit_in_file": n_audit,
        "manifest": wh_manifest,
    }


def _run_async(coro):
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


def export_document_to_warehouse(doc_id: str, *, stamp: date | None = None) -> bool:
    """Best-effort routine export for one finished document (sync graph/API path)."""
    if not doc_id or not warehouse_export_enabled():
        return False
    try:
        result = _run_async(export_to_warehouse(doc_ids=[doc_id], stamp=stamp))
        return bool(result.get("exported_documents"))
    except Exception:
        logger.exception("warehouse_export_document_failed", doc_id=doc_id)
        return False
