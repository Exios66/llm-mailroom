"""Parquet cold store for finished documents + matching audit chains.

Layout under ``{MAILROOM_BASE_DIR}/warehouse/``::

    documents_YYYY-MM-DD.parquet       # terminal-stage catalog rows
    audit_YYYY-MM-DD.parquet           # per-event audit entries for those doc_ids
    audit_rows_YYYY-MM-DD.parquet      # unified rows + audit_row_metrics columns (key entry_id)
    audit_attempts_YYYY-MM-DD.parquet  # their sub-rows (key attempt_id, parent_id)
    audit_addenda_YYYY-MM-DD.parquet   # their addenda (key addendum_id, doc_id)
    audit_legacy.parquet               # the migrated per-event history, written once
    manifest.json                      # schema version, export watermark, doc_id -> entry_hash + files

Join key: ``doc_id``. Routine export runs after archive/failed (best-effort).
Full/incremental backfill: ``scripts/export_warehouse.py``.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from uuid import uuid4
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

WAREHOUSE_SCHEMA_VERSION = "1"
TERMINAL_STAGES = frozenset({"archived", "failed"})
_MANIFEST_NAME = "manifest.json"
_EXPORT_LOCK = threading.RLock()
# Ids per IN clause. Leaves room for other binds under SQLite's 999-variable default
# before 3.32; newer SQLite and asyncpg allow about 32k.
_IN_CLAUSE_CHUNK_SIZE = 500


@contextmanager
def _publication_lock(directory: Path):
    """Serialize complete read/merge/publication transactions across workers."""
    directory.mkdir(parents=True, exist_ok=True)
    with _EXPORT_LOCK, (directory / ".warehouse-export.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            # LK_LOCK gives up after ten retries. Match flock's indefinite
            # contention wait without swallowing invalid-handle/I/O errors.
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno != errno.EACCES:
                        raise
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _replacement_path(path: Path):
    """Publish a complete sibling temporary file; failed writes keep old bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Query kernel-applied creation permissions on an always-empty inode.
    # A reader may retain a descriptor before unlink/chmod, so that inode
    # must never receive payload bytes. Do not change the global umask.
    probe = path.parent / f".{path.name}.{uuid4().hex}.mode.tmp"
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        creation_mode = os.fstat(fd).st_mode & 0o777
    finally:
        os.close(fd)
        probe.unlink(missing_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        yield temporary
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else creation_mode)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def warehouse_export_enabled() -> bool:
    raw = os.environ.get("MAILROOM_WAREHOUSE_EXPORT", "auto").strip().lower()
    if raw in ("0", "false", "no", "off", "disabled"):
        return False
    if raw in ("1", "true", "yes", "on", "enabled"):
        return True
    # auto: enabled when pyarrow is importable
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


def daily_audit_rows_path(stamp: date) -> Path:
    return warehouse_dir() / f"audit_rows_{stamp.isoformat()}.parquet"


def daily_audit_attempts_path(stamp: date) -> Path:
    return warehouse_dir() / f"audit_attempts_{stamp.isoformat()}.parquet"


def daily_audit_addenda_path(stamp: date) -> Path:
    return warehouse_dir() / f"audit_addenda_{stamp.isoformat()}.parquet"


def audit_legacy_path() -> Path:
    return warehouse_dir() / "audit_legacy.parquet"


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
    with _replacement_path(manifest_file()) as temporary:
        temporary.write_text(json.dumps(manifest, indent=2, default=str) + "\n")


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


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def unified_to_rows(full: dict[str, Any]) -> tuple[dict, list[dict], list[dict]]:
    """One ``get_row`` result as its ``audit_rows``, ``audit_attempts`` and ``audit_addenda`` records."""
    from audit.metrics import metrics_for

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "entry_id": full["entry_id"], "doc_id": full["doc_id"], "matter_id": full.get("matter_id"),
        "seq": full.get("seq"), "event": full["event"], "actor": full.get("actor"),
        "timestamp": full.get("timestamp"), "prev_hash": full.get("prev_hash"),
        "entry_hash": full.get("entry_hash"), "detail_json": _json(full.get("detail")),
        **metrics_for(full), "exported_at": now,
    }
    addenda = list(full.get("addenda") or [])
    subs = list(full.get("sub_rows") or []) + [s for a in addenda for s in a.get("sub_rows") or []]
    attempts = [
        {**{k: s.get(k) for k in ("attempt_id", "parent_id", "doc_id", "stage", "attempt_no", "trigger",
                                  "outcome", "started_at", "ended_at", "entry_hash")},
         "detail_json": _json(s.get("detail")), "exported_at": now}
        for s in subs
    ]
    adds = [
        {"addendum_id": a["addendum_id"], "doc_id": full["doc_id"], "kind": a["kind"], "seq": a.get("seq"),
         "actor": a.get("actor"), "timestamp": a.get("timestamp"), "prev_hash": a.get("prev_hash"),
         "entry_hash": a.get("entry_hash"), "detail_json": _json(a.get("detail")), "exported_at": now}
        for a in addenda
    ]
    return row, attempts, adds


async def fetch_unified_rows(doc_ids: list[str]) -> list[dict[str, Any]]:
    """``get_row`` for each id that has a unified row (none before the migration)."""
    from audit.models import AuditMigrationPending
    from audit.writer import get_row

    out = []
    for doc_id in dict.fromkeys(doc_ids):
        try:
            full = await get_row(doc_id)
        except AuditMigrationPending:
            return []
        if full is not None:
            out.append(full)
    return out


async def fetch_legacy_history() -> list[dict[str, Any]] | None:
    """Every ``audit_log_legacy`` entry once the migration has run; ``None`` before it."""
    from sqlalchemy import select

    from storage.audit_log import AuditLegacyRecord, event_log_model
    from storage.db import async_session, ensure_schema

    ensure_schema()
    async with async_session() as session:
        if await event_log_model(session) is not AuditLegacyRecord:
            return None
        records = (await session.execute(select(AuditLegacyRecord))).scalars().all()
    records = sorted(records, key=lambda r: (r.doc_id, r.seq, r.timestamp))
    return [
        {"entry_id": r.entry_id, "doc_id": r.doc_id, "matter_id": r.matter_id, "event": r.event,
         "actor": r.actor, "detail": r.detail, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash,
         "seq": r.seq, "timestamp": r.timestamp}
        for r in records
    ]


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "pyarrow is required for warehouse export (install mailroom with "
            "pyarrow)"
        ) from exc
    return pa, pq


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    pa, pq = _require_pyarrow()
    # Pin one native handle for metadata and pages across atomic replacement.
    # Python file ownership can deadlock Arrow workers during interpreter exit.
    with pa.OSFile(str(path), "rb") as snapshot:
        return pq.read_table(snapshot).to_pylist()


def _merge_rows(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    merged = {}
    for row in [*existing, *incoming]:
        k = row.get(key)
        if k is not None:
            previous = merged.get(str(k))
            # A snapshot loaded before another exporter can acquire the
            # publication lock later. Keep the newest known catalog revision.
            # Unknown revisions cannot supersede known ones. Malformed
            # revisions raise before publication, preserving corrupt evidence.
            if key == "doc_id":
                new = _row_updated_at(row)
                old = _row_updated_at(previous) if previous is not None else None
                if old is not None and (new is None or new < old):
                    continue
            merged[str(k)] = row
    return list(merged.values())


def _row_updated_at(row: dict[str, Any]) -> datetime | None:
    raw = row.get("updated_at")
    if raw is None:
        return None
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def write_parquet_rows(path: Path, rows: list[dict[str, Any]], *, merge_key: str) -> int:
    """Upsert ``rows`` into ``path`` (create or merge by ``merge_key``)."""
    if not rows:
        return 0
    with _publication_lock(path.parent):
        return _write_parquet_rows_unlocked(path, rows, merge_key=merge_key)


def _write_parquet_rows_unlocked(path: Path, rows: list[dict[str, Any]], *, merge_key: str) -> int:
    """Write under the caller's directory transaction lock, without nesting it."""
    if not rows:
        return 0
    pa, pq = _require_pyarrow()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _merge_rows(_read_parquet_rows(path), rows, merge_key)
    table = pa.Table.from_pylist(merged)
    with _replacement_path(path) as temporary:
        pq.write_table(table, temporary, compression="snappy")
    return len(merged)


def _doc_id_chunks(doc_ids: list[str]) -> list[list[str]]:
    """Split ids for IN clauses; dedupe so no row comes back from two chunks."""
    unique = list(dict.fromkeys(doc_ids))
    return [
        unique[start : start + _IN_CLAUSE_CHUNK_SIZE]
        for start in range(0, len(unique), _IN_CLAUSE_CHUNK_SIZE)
    ]


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
            records: list = []
            for chunk in _doc_id_chunks(doc_ids):
                result = await session.execute(
                    select(DocumentRecord).where(
                        DocumentRecord.doc_id.in_(chunk),
                        DocumentRecord.stage.in_(tuple(TERMINAL_STAGES)),
                    )
                )
                records.extend(result.scalars().all())
            return records
        query = select(DocumentRecord).where(
            DocumentRecord.stage.in_(tuple(TERMINAL_STAGES))
        )
        if since is not None:
            query = query.where(DocumentRecord.updated_at > since)
        query = query.order_by(DocumentRecord.updated_at)
        result = await session.execute(query)
        return list(result.scalars().all())


async def fetch_audit_rows_for_doc_ids(doc_ids: list[str]) -> list[dict[str, Any]]:
    """Return per-event audit rows with doc ids, or ``[]`` for no requested ids.

    After migration, read legacy history, excluding unified rows and addenda.
    Up to 50 ids preserve request order with each chain in sequence order;
    larger requests return rows ordered by doc id, sequence, and timestamp.
    Schema initialization may migrate legacy data; database errors propagate.
    """
    if not doc_ids:
        return []
    from sqlalchemy import select

    from storage.audit_log import _hold_read_snapshot, event_log_model, get_audit_chain
    from storage.db import async_session, ensure_schema

    ensure_schema()
    rows: list[dict[str, Any]] = []
    # Per-doc chain preserves seq order; batch read for large sets.
    if len(doc_ids) <= 50:
        for doc_id in doc_ids:
            rows.extend({**row, "doc_id": doc_id} for row in await get_audit_chain(doc_id))
        return rows
    async with async_session() as session:
        await _hold_read_snapshot(session)
        Model = await event_log_model(session)
        records: list[Any] = []
        for chunk in _doc_id_chunks(doc_ids):
            result = await session.execute(select(Model).where(Model.doc_id.in_(chunk)))
            records.extend(result.scalars().all())
        records.sort(key=lambda rec: (rec.doc_id, rec.seq, rec.timestamp))
        for rec in records:
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

    unified = [unified_to_rows(full) for full in await fetch_unified_rows(ids)]
    legacy = None if "legacy_export" in wh_manifest else await fetch_legacy_history()

    doc_path = daily_documents_path(stamp)
    audit_path = daily_audit_path(stamp)
    latest_updated = max(
        (r.updated_at for r in records if r.updated_at is not None),
        default=None,
    )
    n_docs, n_audit, wh_manifest, n_legacy = _publish_export(
        stamp, doc_rows, audit_rows, latest_updated, unified=unified, legacy=legacy
    )

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
        "exported_audit_rows": len(unified),
        "exported_audit_attempts": sum(len(u[1]) for u in unified),
        "exported_audit_addenda": sum(len(u[2]) for u in unified),
        "exported_audit_legacy": n_legacy,
        "total_documents_in_file": n_docs,
        "total_audit_in_file": n_audit,
        "manifest": wh_manifest,
    }


def _publish_unified(stamp, unified, manifest) -> None:
    """Upsert the unified rows, sub-rows and addenda, and map each doc to its files (caller holds the lock)."""
    if not unified:
        return
    paths = (daily_audit_rows_path(stamp), daily_audit_attempts_path(stamp), daily_audit_addenda_path(stamp))
    _write_parquet_rows_unlocked(paths[0], [u[0] for u in unified], merge_key="entry_id")
    _write_parquet_rows_unlocked(paths[1], [a for u in unified for a in u[1]], merge_key="attempt_id")
    _write_parquet_rows_unlocked(paths[2], [a for u in unified for a in u[2]], merge_key="addendum_id")
    docs = manifest.setdefault("documents", {})
    for row, attempts, addenda in unified:
        files = [paths[0].name] + ([paths[1].name] if attempts else []) + ([paths[2].name] if addenda else [])
        docs[row["doc_id"]] = {"entry_hash": row["entry_hash"], "export_date": stamp.isoformat(), "files": files}


def _publish_legacy(legacy, manifest) -> int:
    """Write ``audit_legacy.parquet`` the first time an export sees the migrated history (caller holds the lock)."""
    if legacy is None or "legacy_export" in manifest:
        return 0
    rows = [audit_to_row(e) for e in legacy]
    if rows:
        _write_parquet_rows_unlocked(audit_legacy_path(), rows, merge_key="entry_id")
    manifest["legacy_export"] = {"file": audit_legacy_path().name, "rows": len(rows),
                                 "exported_at": datetime.now(timezone.utc).isoformat()}
    return len(rows)


def _publish_export(stamp, doc_rows, audit_rows, latest_updated, *, unified=(), legacy=None):
    """Commit the daily upserts and fresh manifest state without an await."""
    with _publication_lock(warehouse_dir()):
        doc_path = daily_documents_path(stamp)
        audit_path = daily_audit_path(stamp)
        n_docs = _write_parquet_rows_unlocked(doc_path, doc_rows, merge_key="doc_id")
        n_audit = _write_parquet_rows_unlocked(audit_path, audit_rows, merge_key="entry_id")
        # Load after acquiring the lock: a concurrent exporter may have
        # committed another day or advanced the watermark while DB rows loaded.
        manifest = load_warehouse_manifest()
        _publish_unified(stamp, list(unified), manifest)
        n_legacy = _publish_legacy(legacy, manifest)
        now = datetime.now(timezone.utc).isoformat()
        manifest["last_export_at"] = now
        if latest_updated is not None:
            latest = _utc_iso(latest_updated)
            previous = manifest.get("last_doc_updated_at")
            if previous:
                try:
                    old = datetime.fromisoformat(str(previous).replace("Z", "+00:00"))
                    if old.tzinfo is None:
                        old = old.replace(tzinfo=timezone.utc)
                    if old > datetime.fromisoformat(latest):
                        latest = str(previous)
                except ValueError:
                    logger.warning("warehouse_invalid_previous_watermark")
            manifest["last_doc_updated_at"] = latest
        manifest.setdefault("daily_files", {})[stamp.isoformat()] = {
            "documents": doc_path.name, "audit": audit_path.name,
            "document_count": n_docs, "audit_count": n_audit,
            "last_batch_at": now, "batch_documents": len(doc_rows),
            "batch_audit_entries": len(audit_rows),
        }
        save_warehouse_manifest(manifest)
        return n_docs, n_audit, manifest, n_legacy


def _run_async(coro):
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is running in THIS thread (async API handler). Scheduling onto it
    # and blocking on the future would deadlock it, so run the coroutine on a
    # fresh loop in a helper thread instead.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=30)


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
