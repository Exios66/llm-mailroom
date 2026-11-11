"""The ``sql`` trace sink (spec §5.5): always on.

Full I/O goes to ``audit_trace_io`` in the mailroom database. Pending rows (a row
that could not be committed, §5.6) go to a separate SQLite file,
``MAILROOM_AUDIT_SPOOL_PATH`` (default ``<db dir>/audit_spool.db``), so they
survive the database being the thing that failed.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from audit.digests import io_sha256

logger = structlog.get_logger(__name__)

_SPOOL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pending_rows ("
    " ref TEXT PRIMARY KEY, doc_id TEXT, record TEXT NOT NULL, created_at TEXT NOT NULL)"
)


def _db_dir() -> Path:
    from storage.db import _resolve_url

    url = _resolve_url()
    if url.startswith("sqlite") and "///" in url:
        return Path(url.split("///", 1)[1]).resolve().parent
    return Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()


def spool_path() -> Path:
    """Where pending rows are kept: ``MAILROOM_AUDIT_SPOOL_PATH`` or ``<db dir>/audit_spool.db``."""
    raw = os.environ.get("MAILROOM_AUDIT_SPOOL_PATH")
    return Path(raw).expanduser().resolve() if raw else _db_dir() / "audit_spool.db"


def data_dir() -> Path:
    """The persistent data directory: ``MAILROOM_DATA_DIR``, else the bins' ``MAILROOM_BASE_DIR``."""
    raw = os.environ.get("MAILROOM_DATA_DIR") or os.environ.get("MAILROOM_BASE_DIR", "./data")
    return Path(raw).expanduser().resolve()


def warn_if_ephemeral() -> bool:
    """Log ``audit_spool_ephemeral`` when the spool is outside the data directory (spec §5.6).

    A redeploy (Railway, a fresh container) loses anything outside the mounted
    volume, so pending rows parked there would be lost. Returns whether it warned.
    """
    try:
        path, root = spool_path(), data_dir()
        if path.is_relative_to(root):
            return False
        logger.warning("audit_spool_ephemeral", spool_path=str(path), data_dir=str(root))
        return True
    except Exception:
        logger.warning("audit_spool_check_failed", exc_info=True)
        return False


@contextlib.contextmanager
def _spool():
    """A spool connection that commits on success and is always closed."""
    path = spool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute(_SPOOL_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _run(coro_fn):
    from audit.row import _run as run

    return run(coro_fn)


class SqlSink:
    name = "sql"

    def record_io(self, visit_id: str, doc_id: str | None, prompt: str | None, completion: str | None) -> str | None:
        from storage.audit_log import AuditTraceIORecord
        from storage.db import async_session, ensure_schema

        ensure_schema()

        async def write():
            async with async_session() as session:
                await session.merge(AuditTraceIORecord(
                    visit_id=visit_id, doc_id=str(doc_id or ""), prompt=prompt, completion=completion,
                    sha256=io_sha256(prompt, completion), created_at=datetime.now(timezone.utc),
                ))
                await session.commit()

        _run(write)
        return visit_id

    def fetch_io(self, ref: str) -> dict | None:
        from sqlalchemy import select

        from storage.audit_log import AuditTraceIORecord
        from storage.db import async_session, ensure_schema

        ensure_schema()

        async def read():
            async with async_session() as session:
                rec = (await session.execute(
                    select(AuditTraceIORecord).where(AuditTraceIORecord.visit_id == ref)
                )).scalar_one_or_none()
                if rec is None:
                    return None
                return {"visit_id": rec.visit_id, "doc_id": rec.doc_id, "prompt": rec.prompt,
                        "completion": rec.completion, "sha256": rec.sha256}

        return _run(read)

    def capture_pending(self, record: dict[str, Any]) -> str | None:
        ref = f"pend-{uuid.uuid4().hex}"
        with _spool() as conn:
            conn.execute(
                "INSERT INTO pending_rows (ref, doc_id, record, created_at) VALUES (?, ?, ?, ?)",
                (ref, str(record.get("doc_id") or ""), json.dumps(record, default=str),
                 datetime.now(timezone.utc).isoformat()),
            )
        return ref

    def fetch_pending(self) -> list[dict]:
        path = spool_path()
        if not path.exists():
            return []
        with _spool() as conn:
            rows = conn.execute("SELECT ref, record FROM pending_rows ORDER BY created_at, ref").fetchall()
        return [{"ref": ref, **json.loads(record)} for ref, record in rows]

    def drop_pending(self, ref: str) -> None:
        """Forget a pending row once it is committed (``audit-reconcile``)."""
        if not spool_path().exists():
            return
        with _spool() as conn:
            conn.execute("DELETE FROM pending_rows WHERE ref = ?", (ref,))


__all__ = ["SqlSink", "data_dir", "spool_path", "warn_if_ephemeral"]
