"""On-disk cache of validated LLM results, keyed by model + prompt + input.

The same document re-sent (a forwarded copy, a retry after a reject, a
re-run) gets the same answer without another LLM call. Only validated
results are stored. SQLite at ``<base>/llm_result_cache.sqlite``, entries
expire after ``TTL_SECONDS`` (30 days); ``MAILROOM_LLM_CACHE=0`` disables
both reads and writes. Every operation is best-effort and never raises.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time

import structlog

logger = structlog.get_logger(__name__)

TTL_SECONDS = 30 * 24 * 3600
SWEEP_LIMIT = 100  # expired rows removed per write
_LOCK = threading.Lock()


def cache_enabled() -> bool:
    return str(os.environ.get("MAILROOM_LLM_CACHE", "1")).strip().lower() not in ("0", "false", "no", "off")


def cache_key(model_tag: str, prompt: str, doc_text: str) -> str:
    h = hashlib.sha256()
    for part in (model_tag, prompt, doc_text):
        data = str(part or "").encode("utf-8")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _db_path():
    from pipeline.bins import get_base_dir

    return get_base_dir() / "llm_result_cache.sqlite"


@contextlib.contextmanager
def _db():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        with conn:
            yield conn
    finally:
        conn.close()


def get(key: str, now: float | None = None) -> dict | None:
    if not cache_enabled():
        return None
    now = time.time() if now is None else now
    try:
        with _LOCK, _db() as conn:
            row = conn.execute("SELECT value, created_at FROM results WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if now - float(row[1]) > TTL_SECONDS:
                conn.execute("DELETE FROM results WHERE key=?", (key,))
                return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except Exception:
        logger.warning("llm_result_cache_read_failed", exc_info=True)
        return None


def put(key: str, value: dict, now: float | None = None) -> None:
    if not cache_enabled():
        return
    now = time.time() if now is None else now
    try:
        payload = json.dumps(value, default=str)
        with _LOCK, _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO results (key, value, created_at) VALUES (?,?,?)", (key, payload, now)
            )
            # Bounded sweep so entries that are never read again still expire.
            conn.execute(
                "DELETE FROM results WHERE rowid IN"
                " (SELECT rowid FROM results WHERE created_at < ? LIMIT ?)",
                (now - TTL_SECONDS, SWEEP_LIMIT),
            )
    except Exception:
        logger.warning("llm_result_cache_write_failed", exc_info=True)
