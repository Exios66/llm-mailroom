"""Read-only audit inspection for the diagnostics workbench.

The audit store has two shapes in this constellation, and this module reads
both without assuming the roadmap writers have finished:

* **event chain** (today's ``audit_log``): many hash-chained rows per document,
  sequenced per document. Verified with :func:`schemas.audit.verify_chain`.
* **unified row** (foundation M2/M3, optional): one immutable hash-chained
  ``audit_log`` row per document plus ``audit_attempts`` sub-rows and
  ``audit_addenda``; the previous per-event rows are preserved as
  ``audit_log_legacy``. Verified with :mod:`audit.verify` when the foundation
  exposes a document-scoped helper, otherwise by recomputing the hashes with
  the foundation's own ``audit.writer`` primitives (main row/addenda always,
  attempts only when that module is importable), and the legacy section with
  :func:`schemas.audit.verify_chain`.

Hard boundaries for this module:

* It never imports ``api.main``, the watcher, an LLM client, or the storage
  package. It opens its **own** read-only SQLAlchemy connection from the
  configured storage URL (``DATABASE_URL``, else ``<MAILROOM_BASE_DIR>/mailroom.db``),
  issues ``SELECT`` statements only, and never calls ``ensure_schema``.
* A missing database stays missing: the SQLite file is checked before a
  connection is opened and is opened with ``mode=ro`` + ``query_only``.
* Verification runs on the **full unredacted rows**. Hash inputs are the raw
  stored columns: a non-object ``detail``, a non-text hashed field or an invalid
  attempt timestamp is a structured failure, never a coerced ``{}``/``None``
  that could make a malformed stored value match a hash it was never written
  with. Only then is a strict allowlisted projection built: ids,
  event/actor/times, hash links, derived status, attempt summaries and *decision
  field names*. Raw ``detail`` values never leave this module (they can hold
  document text, secrets or paths).
* Every read is bounded. A truncated read never reports ``verified``.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

AUDIT_SCHEMA_VERSION = "1.0.0"

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 100_000
MAX_EVENTS = 2000
MAX_ATTEMPTS = 500
MAX_ADDENDA = 200
MAX_DECISION_FIELDS = 40
MAX_FIELD_NAME = 64

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Terminal events a unified row may carry (foundation M3 ``EVENTS``). The
# event-chain status mapping is deliberately narrower: an unknown event is
# reported as "recorded", never guessed into a terminal outcome.
_TERMINAL_STATUS = {
    "archived": "archived",
    "triage_archived": "archived",
    "failed": "failed",
    "triage_reviewed": "review",
}
_EVENT_STATUS = {
    **_TERMINAL_STATUS,
    "review_approved": "review",
    "review_rejected": "review",
    "review_completed": "review",
    "review_recorded": "review",
    "review_requeued": "review",
    "routed_to_review": "review",
    "classified": "processing",
    "extracted": "processing",
    "ingested": "processing",
}

Scope = Literal["main", "attempts", "addenda", "legacy", "events"]


class AuditStoreUnavailable(RuntimeError):
    """The store cannot be read. ``code`` is safe to expose; no path or URL."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Store:
    engine: str  # sqlite | postgres
    url: str  # internal only; never serialized


def _configured_url() -> str:
    """Mirror ``storage.db._resolve_url`` without importing the storage package."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()
    return f"sqlite+aiosqlite:///{base / 'mailroom.db'}"


def _sqlite_path(url: str) -> Path | None:
    if "///" not in url:
        return None
    raw = url.split("///", 1)[1].split("?", 1)[0]
    return Path(raw).expanduser() if raw else None


def _sync_postgres_url(url: str) -> str | None:
    """A sync SQLAlchemy URL for an async/plain Postgres storage URL, or None."""
    for driver in ("psycopg", "psycopg2"):
        try:
            importlib.import_module(driver)
        except Exception:
            continue
        if url.startswith("postgresql+asyncpg://"):
            return "postgresql+" + driver + "://" + url.split("://", 1)[1]
        if url.startswith("postgresql://"):
            return "postgresql+" + driver + "://" + url.split("://", 1)[1]
        if url.startswith("postgres://"):
            return "postgresql+" + driver + "://" + url.split("://", 1)[1]
        return None
    return None


def _resolve_store() -> tuple[_Store | None, str | None]:
    """Return a readable store or a safe reason code. Never creates a database."""
    url = _configured_url()
    if url.startswith("sqlite"):
        path = _sqlite_path(url)
        if path is None or not path.is_file():
            return None, "database-missing"
        return _Store("sqlite", url), None
    if url.startswith(("postgresql", "postgres")):
        sync_url = _sync_postgres_url(url)
        if sync_url is None:
            return None, "driver-unavailable"
        return _Store("postgres", sync_url), None
    return None, "unsupported-url"


def _sqlite_creator(path: Path):
    # ``mode=ro`` makes accidental writes fail at the driver level; a missing
    # file is refused here instead of being created (sqlite's default).
    uri = path.resolve().as_uri() + "?mode=ro"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        # Autocommit: this module issues its own BEGIN (see _connection) so one
        # snapshot spans every SELECT. pysqlite's legacy mode never begins a
        # transaction for SELECTs, which would let a writer commit between the
        # main-row and addenda reads and look like a broken chain.
        conn.isolation_level = None
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    return connect


# Test instrumentation only: called with ("begin"|"mid"|"end", connection).
# It exists so a regression test can prove the read transaction was opened
# before the first SELECT and stayed open across the multi-query verification.
_snapshot_probe: Any = None


def _notify(stage: str, conn: Connection) -> None:
    probe = _snapshot_probe
    if probe is None:
        return
    try:
        probe(stage, conn)
    except Exception:
        pass


def _begin_snapshot(conn: Connection, engine: str) -> Any:
    """Open one consistent read-only snapshot before any SELECT.

    SQLite: an explicit deferred ``BEGIN``; the transaction pins the snapshot
    at the first read and holds it until rollback. Postgres: a REPEATABLE READ
    transaction (the engine is read-only), so concurrent commits cannot be
    observed halfway through verification.
    """
    if engine == "sqlite":
        conn.exec_driver_sql("BEGIN")
        return None
    tx = conn.begin()
    return tx


def _end_snapshot(conn: Connection, tx: Any, engine: str) -> None:
    try:
        if engine == "sqlite":
            conn.exec_driver_sql("ROLLBACK")
        elif tx is not None:
            tx.rollback()
    except Exception:
        # A broken connection is closed below; never mask the read error.
        pass


@contextmanager
def _connection() -> Iterator[Connection]:
    store, reason = _resolve_store()
    if store is None:
        raise AuditStoreUnavailable(reason or "database-unavailable")
    engine: Engine
    if store.engine == "sqlite":
        path = _sqlite_path(_configured_url())
        assert path is not None
        engine = create_engine(
            "sqlite://", creator=_sqlite_creator(path), poolclass=NullPool
        )
    else:
        engine = create_engine(
            store.url,
            poolclass=NullPool,
            isolation_level="REPEATABLE READ",
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
    try:
        with engine.connect() as conn:
            tx = _begin_snapshot(conn, store.engine)
            _notify("begin", conn)
            try:
                yield conn
            finally:
                _end_snapshot(conn, tx, store.engine)
    finally:
        engine.dispose()


def valid_doc_id(doc_id: str) -> bool:
    return bool(_ID.fullmatch(doc_id or ""))


# ------------------------------------------------------------------ helpers


def _iso(value: Any) -> str | None:
    """UTC ISO string for datetime / stored text, or None. Invalid text → None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt.isoformat()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return dt.isoformat()
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _as_str(value: Any, *, limit: int = 128) -> str | None:
    """A trimmed, capped string for display only.

    Never feed its result to a hash or an identity comparison: hashed columns
    must be verified exactly as the writer stored them (see :func:`_text`).
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)[:limit]
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit] or None


def _text(value: Any) -> str | None:
    """The stored string exactly as written, or None when it is not a string.

    This is the hash-input and identity variant of :func:`_as_str`: the writer
    hashed the raw column, so trimming or truncating here would let a padded
    ``actor`` (or id, hash link, ...) verify against the original digest.
    """
    return value if isinstance(value, str) else None


def _hash_timestamp(value: Any, *, optional: bool) -> tuple[str | None, str | None]:
    """UTC-normalized timestamp for hashing, or a safe failure reason.

    The writer normalizes naive values to UTC before hashing, so doing the same
    keeps legitimate records verifiable. ``None`` stays ``None`` when the column
    is nullable (the foundation hasher turns it into the empty "unknown time"
    value); anything it would have rejected is a failure here too, because a
    stored invalid non-null timestamp must never collapse to that same value.
    """
    if value is None:
        return (None, None) if optional else (None, "timestamp-missing")
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt.isoformat(), None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None, "timestamp-invalid"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, "timestamp-invalid"
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return dt.isoformat(), None
    return None, "timestamp-invalid"


def _hash(value: Any) -> str | None:
    text_value = _as_str(value, limit=128)
    if not text_value:
        return None
    return text_value if re.fullmatch(r"[0-9a-fA-F]{0,128}", text_value) else None


def _decision_fields(detail: Any) -> list[str]:
    """Key names only: values may hold document text, paths or secrets."""
    if not isinstance(detail, dict):
        return []
    names = sorted(
        name
        for name in detail
        if isinstance(name, str) and 0 < len(name) <= MAX_FIELD_NAME and name.isprintable()
    )
    return names[:MAX_DECISION_FIELDS]


def _row_dict(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", None)
    data = dict(mapping) if mapping is not None else dict(row)
    # JSON columns come back as text on a raw SQLite connection. Verification
    # hashes the decoded object, so decode once here (never expose it raw).
    for key in ("detail", "value"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except ValueError:
                pass
    return data


def _engine_name(conn: Connection) -> str:
    dialect = getattr(conn, "dialect", None)
    name = getattr(dialect, "name", "") if dialect is not None else ""
    return "postgres" if name == "postgresql" else (name or "unknown")


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def _schema_mode(
    conn: Connection, tables: set[str], migration: dict[str, Any] | None = None
) -> str:
    """One of ``unified-row`` / ``event-chain`` / ``ambiguous``.

    Auxiliary table presence is NOT enough: the foundation's ``ensure_schema``
    creates ``audit_attempts``/``audit_addenda``/``audit_meta`` even while the
    migration is pending and ``audit_log`` still holds many per-event rows per
    document. The authoritative signals are the ``audit_meta`` migration marker
    and a unique ``doc_id`` index on ``audit_log``.

    A marker that exists but cannot be parsed is ambiguous: interpreting the
    rows as either shape could read one arbitrary event as a terminal row, so
    this function refuses to guess.
    """
    migration = migration if migration is not None else _migration_info(conn, tables)
    if migration is not None:
        if migration.get("migrated") is None:
            return "ambiguous"
        if migration.get("migrated"):
            return "unified-row"
    inspector = inspect(conn)
    try:
        for constraint in inspector.get_unique_constraints("audit_log"):
            if list(constraint.get("column_names") or []) == ["doc_id"]:
                return "unified-row"
        for index in inspector.get_indexes("audit_log"):
            if index.get("unique") and list(index.get("column_names") or []) == ["doc_id"]:
                return "unified-row"
    except Exception:
        return "event-chain"
    return "event-chain"


def _count(conn: Connection, table: str, where: str, params: dict[str, Any]) -> int:
    value = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).scalar()
    return int(value or 0)


# -------------------------------------------------------------- projection


def _project_entry(row: Any, *, source: str) -> dict[str, Any]:
    data = _row_dict(row)
    event = _as_str(data.get("event"), limit=64)
    actor = _as_str(data.get("actor"), limit=64)
    detail = data.get("detail")
    try:
        if isinstance(detail, str):
            detail = json.loads(detail)
    except ValueError:
        detail = None
    return {
        "source": source,
        "entry_id": _as_str(data.get("entry_id")),
        "event": event,
        "actor": actor,
        "matter_id": _as_str(data.get("matter_id")),
        "timestamp": _iso(data.get("timestamp")),
        "seq": _as_int(data.get("seq")),
        "prev_hash": _hash(data.get("prev_hash")),
        "entry_hash": _hash(data.get("entry_hash")),
        "status": _EVENT_STATUS.get(event or "", "recorded"),
        "decision_fields": _decision_fields(detail),
    }


def _project_attempt(row: Any) -> dict[str, Any]:
    data = _row_dict(row)
    detail = data.get("detail")
    try:
        if isinstance(detail, str):
            detail = json.loads(detail)
    except ValueError:
        detail = None
    stage = _as_str(data.get("stage"), limit=32)
    return {
        "attempt_id": _as_str(data.get("attempt_id")),
        "stage": stage if stage and _STAGE.fullmatch(stage) else None,
        "attempt_no": _as_int(data.get("attempt_no")),
        "trigger": _as_str(data.get("trigger"), limit=32),
        "outcome": _as_str(data.get("outcome"), limit=64),
        "started_at": _iso(data.get("started_at")),
        "ended_at": _iso(data.get("ended_at")),
        "entry_hash": _hash(data.get("entry_hash")),
        "decision_fields": _decision_fields(detail),
    }


def _project_addendum(row: Any, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    data = _row_dict(row)
    detail = data.get("detail")
    try:
        if isinstance(detail, str):
            detail = json.loads(detail)
    except ValueError:
        detail = None
    return {
        "addendum_id": _as_str(data.get("addendum_id")),
        "kind": _as_str(data.get("kind"), limit=32),
        "seq": _as_int(data.get("seq")),
        "actor": _as_str(data.get("actor"), limit=64),
        "timestamp": _iso(data.get("timestamp")),
        "prev_hash": _hash(data.get("prev_hash")),
        "entry_hash": _hash(data.get("entry_hash")),
        "decision_fields": _decision_fields(detail),
        "attempts": attempts,
    }


def _store_summary(
    engine: str,
    schema: str,
    tables: set[str],
    migration: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "state": "available",
        "engine": engine,
        "schema": schema,
        "tables": sorted(
            tables & {"audit_log", "audit_log_legacy", "audit_attempts", "audit_addenda", "audit_meta"}
        ),
        "migration": migration,
    }


def _migration_info(conn: Connection, tables: set[str]) -> dict[str, Any] | None:
    if "audit_meta" not in tables:
        return None
    try:
        row = conn.execute(
            text("SELECT value FROM audit_meta WHERE key = :key"), {"key": "migration"}
        ).first()
    except Exception:
        return {
            "migrated": None,
            "legacy_rows": None,
            "migrated_at": None,
            "reason": "migration-marker-unreadable",
        }
    if row is None:
        return {"migrated": False, "legacy_rows": None, "migrated_at": None}
    value = _row_dict(row).get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        # A marker we cannot parse must never be read as "migrated": doing so
        # would present one arbitrary event row of a still-per-event store as a
        # verified terminal row. ``migrated: None`` tells the caller to refuse
        # both interpretations instead of falling back to the event chain.
        return {
            "migrated": None,
            "legacy_rows": None,
            "migrated_at": None,
            "reason": "migration-marker-invalid",
        }
    if "migrated" in value and value.get("migrated") is not True:
        # The foundation's marker has no ``migrated`` field (its presence means
        # the migration ran), but an explicit ``migrated: false`` says the
        # opposite. Trusting any dict would read a per-event store as unified,
        # so an explicit non-true flag fails closed as ambiguous.
        return {
            "migrated": None,
            "legacy_rows": None,
            "migrated_at": None,
            "reason": "migration-marker-invalid",
        }
    return {
        "migrated": True,
        "legacy_rows": _as_int(value.get("legacy_rows")),
        "migrated_at": _iso(value.get("migrated_at")),
    }


# ------------------------------------------------------------- verification


def _foundation_writer() -> tuple[Any, Any] | tuple[None, None]:
    """The foundation's own hashing primitives, when that package is installed."""
    try:
        writer = importlib.import_module("audit.writer")
        hash_entry = getattr(writer, "hash_entry", None)
        attempt_hash = getattr(writer, "attempt_hash", None)
        if callable(hash_entry) and callable(attempt_hash):
            return hash_entry, attempt_hash
    except Exception:
        pass
    return None, None


def _recompute_row_hash(
    row: dict[str, Any], *, hash_entry: Any | None
) -> tuple[str | None, str | None]:
    """Recompute a stored row hash from the raw columns.

    Returns ``(hash, None)`` or ``(None, reason)``. The lookup fields are
    hashed exactly as stored and a stored ``detail`` must be a JSON object:
    stripping a column would let a whitespace-padded actor verify against the
    original digest, and coercing a non-object detail to ``{}`` would make a
    nil/list/scalar value verify against a hash the writer computed for ``{}``.
    """
    detail = row.get("detail")
    if not isinstance(detail, dict):
        return None, "detail-not-object"
    values: dict[str, str] = {}
    for field in ("prev_hash", "doc_id", "entry_id", "event", "matter_id", "actor"):
        value = _text(row.get(field))
        if value is None:
            return None, "field-not-text"
        values[field] = value
    timestamp, reason = _hash_timestamp(row.get("timestamp"), optional=False)
    if reason is not None:
        return None, reason
    try:
        if hash_entry is not None:
            return hash_entry(
                prev_hash=values["prev_hash"],
                doc_id=values["doc_id"],
                entry_id=values["entry_id"],
                event=values["event"],
                detail=detail,
                matter_id=values["matter_id"],
                actor=values["actor"],
                timestamp=timestamp,
            ), None
        from schemas.audit import compute_audit_hash

        return compute_audit_hash(
            values["prev_hash"],
            values["doc_id"],
            values["entry_id"],
            values["event"],
            detail,
            matter_id=values["matter_id"],
            actor=values["actor"],
            timestamp=timestamp,
        ), None
    except Exception:
        return None, "hash-unavailable"


def _addendum_hash_input(addendum: dict[str, Any], *, matter_id: Any) -> dict[str, Any]:
    """Map an addendum row onto the terminal-row hash input shape.

    The foundation hashes an addendum with ``entry_id = addendum_id`` and
    ``event = kind``; the matter id comes from the document's row. All values
    stay raw: :func:`_recompute_row_hash` validates their types and hashes them
    exactly as stored.
    """
    return {
        "entry_id": addendum.get("addendum_id"),
        "addendum_id": addendum.get("addendum_id"),
        "doc_id": addendum.get("doc_id"),
        "event": addendum.get("kind"),
        "actor": addendum.get("actor"),
        "matter_id": matter_id,
        "timestamp": addendum.get("timestamp"),
        "prev_hash": addendum.get("prev_hash"),
        "entry_hash": addendum.get("entry_hash"),
        "detail": addendum.get("detail"),
    }


def _listed_attempts(detail: Any) -> list[dict[str, Any]] | None:
    if not isinstance(detail, dict):
        return None
    listed = detail.get("attempts")
    if listed is None:
        return []
    if not isinstance(listed, list):
        return None
    return [item for item in listed if isinstance(item, dict)]


def _verify_attempts(
    *,
    parents: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    attempts_available: bool,
    attempt_hash: Any | None,
    failures: list[dict[str, Any]],
    checked: dict[str, int],
    notes: list[str],
    addenda_truncated: bool = False,
) -> bool:
    """Account for every attempt row and every parent reference exactly once.

    Returns True only when every sub-row was hash-verified. Structural problems
    (orphans, cross-document parentage, malformed or duplicate references,
    unlisted rows) are failures: filtering attempts by "known parent" and
    dropping the rest would let a bogus sub-row ride along a verified record.

    Every value entering the hash is the raw stored value. Display trimming
    (:func:`_as_str`) is deliberately not used here, and a stored non-object
    ``detail`` or invalid timestamp is reported instead of being coerced into
    the shape the writer may have hashed.
    """
    if not attempts_available:
        notes.append("attempt sub-rows are not stored in this schema; they are not verified")
        return False

    parent_by_id: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, str | None]] = {}
    for parent in parents:
        parent_id = _text(parent.get("parent_id"))
        if not parent_id:
            failures.append({"scope": "attempts", "id": None, "reason": "parent-id-unavailable"})
            continue
        parent_by_id[parent_id] = parent
        refs[parent_id] = {}
        detail = parent.get("detail")
        if not isinstance(detail, dict):
            # The parent's own hash check reports the non-object detail; without
            # an object there is no reference list to account for.
            continue
        listed = detail.get("attempts")
        if listed is None:
            continue
        if not isinstance(listed, list):
            failures.append({"scope": "attempts", "id": parent_id, "reason": "attempt-list-malformed"})
            continue
        for ref in listed:
            attempt_id = _text(ref.get("attempt_id")) if isinstance(ref, dict) else None
            if attempt_id is None:
                failures.append({"scope": "attempts", "id": parent_id, "reason": "attempt-ref-malformed"})
                continue
            if attempt_id in refs[parent_id]:
                failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-ref-duplicate"})
                continue
            refs[parent_id][attempt_id] = _text(ref.get("entry_hash"))

    by_id: dict[str, dict[str, Any]] = {}
    for item in attempts:
        attempt_id = _text(item.get("attempt_id"))
        if attempt_id is None:
            failures.append({"scope": "attempts", "id": None, "reason": "attempt-id-unavailable"})
            continue
        if attempt_id in by_id:
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-duplicate"})
            continue
        by_id[attempt_id] = item

    fully_verified = attempt_hash is not None
    unread_parents = 0
    for attempt_id, item in by_id.items():
        parent_id = _text(item.get("parent_id"))
        parent = parent_by_id.get(parent_id) if parent_id is not None else None
        if parent is None:
            if addenda_truncated:
                # The parent may be an addendum beyond the bounded read. This
                # read cannot distinguish that from a true orphan, so the
                # honest verdict is incomplete evidence: never a fabricated
                # failure, and never "verified".
                unread_parents += 1
                fully_verified = False
                continue
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-orphan"})
            continue
        if _text(item.get("doc_id")) != parent.get("doc_id"):
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-doc-mismatch"})
            continue
        if attempt_id not in refs.get(parent_id, {}):
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-not-listed"})
            continue
        detail = item.get("detail")
        if not isinstance(detail, dict):
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "detail-not-object"})
            continue
        if attempt_hash is None:
            continue
        started_at, reason = _hash_timestamp(item.get("started_at"), optional=True)
        if reason is None:
            ended_at, reason = _hash_timestamp(item.get("ended_at"), optional=True)
        if reason is not None:
            failures.append({"scope": "attempts", "id": attempt_id, "reason": reason})
            continue
        parent_matter_id = parent.get("matter_id")
        parent_actor = parent.get("actor")
        if not isinstance(parent_matter_id, str) or not isinstance(parent_actor, str):
            failures.append(
                {"scope": "attempts", "id": attempt_id, "reason": "parent-context-invalid"}
            )
            continue
        sub = {
            "attempt_id": item.get("attempt_id"),
            "parent_id": item.get("parent_id"),
            "doc_id": item.get("doc_id"),
            "stage": item.get("stage"),
            "attempt_no": item.get("attempt_no"),
            "trigger": item.get("trigger"),
            "outcome": item.get("outcome"),
            # Normalized exactly as the foundation writer does; timestamps are
            # the one field whose canonical UTC form is part of the hash.
            "started_at": started_at,
            "ended_at": ended_at,
            "detail": detail,
        }
        try:
            expected = attempt_hash(
                sub,
                matter_id=parent_matter_id,
                actor=parent_actor,
            )
        except Exception:
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-hash-unavailable"})
            fully_verified = False
            continue
        if expected != item.get("entry_hash"):
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "hash-mismatch"})
            continue
        if refs[parent_id][attempt_id] != item.get("entry_hash"):
            failures.append({"scope": "attempts", "id": attempt_id, "reason": "parent-hash-mismatch"})
            continue
        checked["attempts"] += 1

    # Every reference must resolve to an attempt that names that exact parent.
    for parent_id, entries in refs.items():
        for attempt_id in entries:
            item = by_id.get(attempt_id)
            if item is None:
                failures.append({"scope": "attempts", "id": attempt_id, "reason": "attempt-missing"})
            elif _text(item.get("parent_id")) != parent_id:
                failures.append(
                    {"scope": "attempts", "id": attempt_id, "reason": "attempt-ref-parent-mismatch"}
                )

    if unread_parents:
        notes.append(
            f"{unread_parents} attempt sub-row(s) may name a parent addendum beyond the bounded "
            "read; they are not verified and are not reported as orphans"
        )
    if attempt_hash is None:
        notes.append("attempt sub-rows not verified: the foundation attempt hasher is unavailable")
    return fully_verified


def _verify_unified(
    *,
    row: dict[str, Any] | None,
    predecessor: dict[str, Any] | None,
    attempts: list[dict[str, Any]],
    addenda: list[dict[str, Any]],
    legacy: list[dict[str, Any]],
    truncated: list[str],
    matter_id: Any,
    attempts_available: bool = True,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    notes: list[str] = []
    checked = {"main": 0, "attempts": 0, "addenda": 0, "legacy": 0}

    hash_entry, attempt_hash = _foundation_writer()
    notes.append(
        "hash recomputation: audit.writer.hash_entry/attempt_hash"
        if hash_entry is not None
        else "hash recomputation: schemas.audit.compute_audit_hash (attempt sub-rows unverified)"
    )

    row_id = _as_str(row.get("entry_id")) if row is not None else None
    if row is None:
        # No terminal row: only the migrated legacy event chain can be verified.
        pass
    else:
        checked["main"] = 1
        expected, reason = _recompute_row_hash(row, hash_entry=hash_entry)
        if reason is not None:
            failures.append({"scope": "main", "id": row_id, "reason": reason})
        elif expected != row.get("entry_hash"):
            failures.append({"scope": "main", "id": row_id, "reason": "hash-mismatch"})
        row_seq = _as_int(row.get("seq"))
        if predecessor is None:
            if row.get("prev_hash") != "":
                failures.append({"scope": "main", "id": row_id, "reason": "chain-start-mismatch"})
            if row_seq != 1:
                failures.append({"scope": "main", "id": row_id, "reason": "seq-start-mismatch"})
        else:
            if row.get("prev_hash") != predecessor.get("entry_hash"):
                failures.append({"scope": "main", "id": row_id, "reason": "prev-hash-mismatch"})
            predecessor_seq = _as_int(predecessor.get("seq"))
            if row_seq is None or predecessor_seq is None or row_seq != predecessor_seq + 1:
                failures.append({"scope": "main", "id": row_id, "reason": "seq-gap"})
        notes.append(
            "main row: v2 hash, immediate predecessor link and seq (not a whole-database chain proof)"
        )

    parents: list[dict[str, Any]] = []
    if row is not None and row_id:
        parents.append(
            {
                "parent_id": row_id,
                "doc_id": row.get("doc_id"),
                "matter_id": matter_id if matter_id is not None else row.get("matter_id"),
                "actor": row.get("actor"),
                "detail": row.get("detail"),
            }
        )

    previous = row.get("entry_hash") if row is not None else None
    expected_seq = 1
    for addendum in addenda:
        checked["addenda"] += 1
        addendum_id = _as_str(addendum.get("addendum_id"))
        data = _addendum_hash_input(addendum, matter_id=matter_id)
        expected, reason = _recompute_row_hash(data, hash_entry=hash_entry)
        if reason is not None:
            failures.append({"scope": "addenda", "id": addendum_id, "reason": reason})
        elif expected != data.get("entry_hash"):
            failures.append({"scope": "addenda", "id": addendum_id, "reason": "hash-mismatch"})
        if previous is not None and data.get("prev_hash") != previous:
            failures.append({"scope": "addenda", "id": addendum_id, "reason": "prev-hash-mismatch"})
        if _as_int(addendum.get("seq")) != expected_seq:
            failures.append({"scope": "addenda", "id": addendum_id, "reason": "seq-gap"})
        expected_seq += 1
        previous = data.get("entry_hash")
        parents.append(
            {
                "parent_id": addendum_id,
                "doc_id": addendum.get("doc_id"),
                "matter_id": matter_id,
                "actor": addendum.get("actor"),
                "detail": addendum.get("detail"),
            }
        )
    if row is not None:
        notes.append("addenda: per-document chain from the row hash with contiguous seq from 1")

    if "attempts" in truncated:
        notes.append("attempt sub-rows exceed the bounded read; they are not verified")
        attempts_verified = False
    else:
        attempts_verified = _verify_attempts(
            parents=parents,
            attempts=attempts,
            attempts_available=attempts_available,
            attempt_hash=attempt_hash,
            failures=failures,
            checked=checked,
            notes=notes,
            addenda_truncated="addenda" in truncated,
        )

    if legacy:
        legacy_failures = _verify_legacy_chain(legacy)
        checked["legacy"] = len(legacy)
        failures.extend(legacy_failures)

    if failures:
        status = "failed"
    elif not attempts_verified and bool(attempts or _lists_attempts(parents)):
        status = "unverified"
    else:
        status = "verified"
    if truncated:
        if status != "failed":
            status = "unverified"
        notes.append("bounded read truncated; hashes are not verified over the whole record")
    if row is None and not legacy:
        status = "not-recorded"
    return {
        "status": status,
        "method": "audit.writer" if hash_entry is not None else "schemas.audit",
        "checked": checked,
        "failures": failures[:50],
        "truncated": bool(truncated),
        "notes": notes,
    }


def _lists_attempts(parents: list[dict[str, Any]]) -> bool:
    for parent in parents:
        detail = parent.get("detail")
        if isinstance(detail, dict) and detail.get("attempts"):
            return True
    return False


def _legacy_entries(
    rows: list[dict[str, Any]], *, scope: str = "legacy"
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Build verifiable entries from stored rows, reporting malformed ones.

    A stored ``detail`` must be a JSON object and every hashed column a string;
    such a row is a failure, never silently hashed as ``{}`` or dropped. The
    raw values are passed through so the chain is checked against the bytes the
    writer hashed.
    """
    from schemas.audit import AuditLogEntry

    entries: list[Any] = []
    failures: list[dict[str, Any]] = []
    for row in rows:
        entry_id = _as_str(row.get("entry_id"))
        detail = row.get("detail")
        if not isinstance(detail, dict):
            failures.append({"scope": scope, "id": entry_id, "reason": "detail-not-object"})
            continue
        timestamp_text, reason = _hash_timestamp(row.get("timestamp"), optional=False)
        if reason is not None:
            failures.append({"scope": scope, "id": entry_id, "reason": reason})
            continue
        values: dict[str, str] = {}
        for field in ("entry_id", "doc_id", "matter_id", "event", "actor", "prev_hash", "entry_hash"):
            value = _text(row.get(field))
            if value is None:
                break
            values[field] = value
        if len(values) != 7:
            failures.append({"scope": scope, "id": entry_id, "reason": "field-not-text"})
            continue
        try:
            entries.append(
                AuditLogEntry(
                    entry_id=values["entry_id"],
                    doc_id=values["doc_id"],
                    matter_id=values["matter_id"],
                    event=values["event"],
                    actor=values["actor"],
                    detail=detail,
                    prev_hash=values["prev_hash"],
                    entry_hash=values["entry_hash"],
                    timestamp=datetime.fromisoformat(timestamp_text),
                )
            )
        except Exception:
            failures.append({"scope": scope, "id": entry_id, "reason": "entry-invalid"})
    return entries, failures


def _verify_legacy_chain(rows: list[dict[str, Any]], *, scope: str = "legacy") -> list[dict[str, Any]]:
    """Verify a migrated/live event chain; return structured failures ([] = ok).

    A row that cannot be represented as an entry is a failure, not a reason to
    drop it: a chain missing one of its records can never be reported verified.
    """
    from schemas.audit import verify_chain

    entries, failures = _legacy_entries(rows, scope=scope)
    if failures:
        return failures
    if len(entries) != len(rows):
        return [{"scope": scope, "id": None, "reason": "entry-invalid"}]
    if verify_chain(entries):
        return []
    return [{"scope": scope, "id": None, "reason": "hash-mismatch"}]


# ---------------------------------------------------------------- readers


def _listing_row(
    conn: Connection,
    doc_id: str,
    latest: Any,
    *,
    mode: str,
    has_legacy: bool,
) -> dict[str, Any]:
    """One listing row: ids and metadata only, no audit detail values."""
    if mode == "ambiguous":
        # The schema cannot be interpreted, so no column is presented as a
        # terminal event; only the bounded row counts are reported.
        entries = _count(conn, "audit_log", "doc_id = :doc", {"doc": doc_id})
        if has_legacy:
            entries += _count(conn, "audit_log_legacy", "doc_id = :doc", {"doc": doc_id})
        return {
            "doc_id": doc_id,
            "matter_id": None,
            "event": None,
            "actor": None,
            "timestamp": _iso(latest),
            "seq": None,
            "entries": entries,
            "source": "unknown",
        }
    if mode == "unified-row":
        row = conn.execute(
            text(
                "SELECT matter_id, event, actor, seq FROM audit_log "
                "WHERE doc_id = :doc LIMIT 1"
            ),
            {"doc": doc_id},
        ).first()
        if row is not None:
            data = _row_dict(row)
            return {
                "doc_id": doc_id,
                "matter_id": _as_str(data.get("matter_id")),
                "event": _as_str(data.get("event"), limit=64),
                "actor": _as_str(data.get("actor"), limit=64),
                "timestamp": _iso(latest),
                "seq": _as_int(data.get("seq")),
                # Counted rather than assumed: a marker-only store without a
                # unique doc_id index can hold duplicate terminal rows, and the
                # listing must not report that as one entry.
                "entries": _count(conn, "audit_log", "doc_id = :doc", {"doc": doc_id}),
                "source": "row",
            }
        return {
            "doc_id": doc_id,
            "matter_id": None,
            "event": None,
            "actor": None,
            "timestamp": _iso(latest),
            "seq": None,
            "entries": _count(conn, "audit_log_legacy", "doc_id = :doc", {"doc": doc_id}),
            "source": "legacy",
        }
    entries = _count(conn, "audit_log", "doc_id = :doc", {"doc": doc_id})
    if entries:
        last = conn.execute(
            text(
                "SELECT event FROM audit_log WHERE doc_id = :doc "
                "ORDER BY seq DESC, timestamp DESC LIMIT 1"
            ),
            {"doc": doc_id},
        ).first()
        return {
            "doc_id": doc_id,
            "matter_id": None,
            "event": _as_str(_row_dict(last).get("event"), limit=64) if last is not None else None,
            "actor": None,
            "timestamp": _iso(latest),
            "seq": None,
            "entries": entries,
            "source": "event-chain",
        }
    return {
        "doc_id": doc_id,
        "matter_id": None,
        "event": None,
        "actor": None,
        "timestamp": _iso(latest),
        "seq": None,
        "entries": _count(conn, "audit_log_legacy", "doc_id = :doc", {"doc": doc_id}),
        "source": "legacy",
    }


def list_documents(limit: int = DEFAULT_LIST_LIMIT, offset: int = 0) -> dict[str, Any]:
    """Bounded document listing from the audit tables (newest first)."""
    safe_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    safe_offset = max(0, min(int(offset), MAX_LIST_OFFSET))
    payload: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "read_only": True,
        "pagination": {"limit": safe_limit, "offset": safe_offset, "returned": 0, "more": False},
        "documents": [],
        "notes": [],
    }
    try:
        with _connection() as conn:
            tables = _tables(conn)
            if "audit_log" not in tables:
                payload["store"] = {
                    "state": "unavailable",
                    "reason": "schema-absent",
                    "engine": None,
                    "schema": None,
                    "tables": sorted(tables & {"audit_log", "audit_log_legacy"}),
                    "migration": None,
                }
                return payload
            mode = _schema_mode(conn, tables)
            migration = _migration_info(conn, tables)
            payload["store"] = _store_summary(_engine_name(conn), mode, tables, migration)
            if mode == "ambiguous":
                payload["notes"].append(
                    "the migration marker is unreadable; the store schema is ambiguous, "
                    "so no column is presented as a terminal record"
                )
            # One UNION listing keeps pagination and "more" correct when a
            # migrated store holds both rows and legacy-only documents.
            if "audit_log_legacy" in tables:
                id_source = (
                    "SELECT doc_id, timestamp FROM audit_log "
                    "UNION ALL SELECT doc_id, timestamp FROM audit_log_legacy"
                )
            else:
                id_source = "SELECT doc_id, timestamp FROM audit_log"
            result = conn.execute(
                text(
                    f"SELECT t.doc_id AS doc_id, MAX(t.timestamp) AS latest FROM ({id_source}) t "
                    "GROUP BY t.doc_id ORDER BY latest DESC, t.doc_id ASC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": safe_limit + 1, "offset": safe_offset},
            )
            rows = [_row_dict(row) for row in result]
            more = len(rows) > safe_limit
            documents: list[dict[str, Any]] = []
            for data in rows[:safe_limit]:
                doc_id = _as_str(data.get("doc_id"))
                if not doc_id:
                    continue
                documents.append(
                    _listing_row(
                        conn,
                        doc_id,
                        data.get("latest"),
                        mode=mode,
                        has_legacy="audit_log_legacy" in tables,
                    )
                )
            payload["documents"] = documents
            payload["pagination"].update({"returned": len(documents), "more": more})
            if "audit_log_legacy" in tables:
                payload["notes"].append(
                    "listing merges live rows with the migrated legacy section; each document appears once"
                )
            return payload
    except AuditStoreUnavailable as exc:
        payload["store"] = {
            "state": "unavailable",
            "reason": exc.code,
            "engine": None,
            "schema": None,
            "tables": [],
            "migration": None,
        }
        return payload
    except Exception:
        payload["store"] = {
            "state": "unavailable",
            "reason": "query-failed",
            "engine": None,
            "schema": None,
            "tables": [],
            "migration": None,
        }
        payload["notes"].append(
            "the audit store could not be read; the underlying error is not "
            "included in this read-only response"
        )
        return payload


def document_audit(doc_id: str) -> dict[str, Any]:
    """One document's audit record(s), verification and redacted projection."""
    if not valid_doc_id(doc_id):
        raise ValueError("invalid document id")
    payload: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "read_only": True,
        "doc_id": doc_id,
        "store": None,
        "record": {"state": "unavailable"},
        "legacy": {"state": "absent", "count": 0, "truncated": False, "entries": []},
        "verification": {
            "status": "unavailable",
            "method": None,
            "checked": {"main": 0, "attempts": 0, "addenda": 0, "legacy": 0},
            "failures": [],
            "truncated": False,
            "notes": [],
        },
        "limits": {
            "events": MAX_EVENTS,
            "attempts": MAX_ATTEMPTS,
            "addenda": MAX_ADDENDA,
            "decision_fields": MAX_DECISION_FIELDS,
        },
        "notes": [],
    }
    try:
        with _connection() as conn:
            tables = _tables(conn)
            if "audit_log" not in tables:
                payload["store"] = {
                    "state": "unavailable",
                    "reason": "schema-absent",
                    "engine": None,
                    "schema": None,
                    "tables": [],
                    "migration": None,
                }
                payload["record"] = {"state": "unavailable", "reason": "schema-absent"}
                payload["verification"]["notes"].append("the audit schema is absent")
                return payload
            mode = _schema_mode(conn, tables)
            migration = _migration_info(conn, tables)
            payload["store"] = _store_summary(_engine_name(conn), mode, tables, migration)
            if mode == "ambiguous":
                # Never guess: an unreadable marker could mean a per-event store
                # read as one terminal row per document (or the reverse).
                reason = (migration or {}).get("reason") or "schema-ambiguous"
                payload["record"] = {"state": "unavailable", "reason": reason}
                payload["verification"]["status"] = "unverified"
                payload["verification"]["notes"].append(
                    "the migration marker is unreadable; the store schema is ambiguous, "
                    "so no row is interpreted or verified"
                )
                payload["notes"].append(
                    "schema-ambiguous: refusing to read the store as unified-row or event-chain"
                )
                return payload
            legacy_rows, legacy_total = _read_legacy(conn, doc_id, tables)
            if mode == "unified-row":
                _read_unified(conn, doc_id, tables, legacy_rows, legacy_total, payload)
            else:
                _read_event_chain(conn, doc_id, legacy_rows, legacy_total, payload)
            return payload
    except AuditStoreUnavailable as exc:
        payload["store"] = {
            "state": "unavailable",
            "reason": exc.code,
            "engine": None,
            "schema": None,
            "tables": [],
            "migration": None,
        }
        payload["record"] = {"state": "unavailable", "reason": exc.code}
        payload["verification"]["notes"].append("the audit store is unavailable")
        return payload
    except Exception:
        payload["store"] = {
            "state": "unavailable",
            "reason": "query-failed",
            "engine": None,
            "schema": None,
            "tables": [],
            "migration": None,
        }
        payload["record"] = {"state": "unavailable", "reason": "query-failed"}
        payload["verification"]["notes"].append(
            "the audit store could not be read; the underlying error is not "
            "included in this read-only response"
        )
        return payload


def _read_legacy(
    conn: Connection, doc_id: str, tables: set[str]
) -> tuple[list[dict[str, Any]], int]:
    if "audit_log_legacy" not in tables:
        return [], 0
    total = _count(conn, "audit_log_legacy", "doc_id = :doc", {"doc": doc_id})
    if total == 0:
        return [], 0
    result = conn.execute(
        text(
            "SELECT * FROM audit_log_legacy WHERE doc_id = :doc "
            "ORDER BY seq, timestamp, entry_id LIMIT :limit"
        ),
        {"doc": doc_id, "limit": MAX_EVENTS},
    )
    return [_row_dict(row) for row in result], total


def _legacy_projection(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    entries = [_project_entry(row, source="legacy") for row in rows]
    return {
        "state": "present" if total else "absent",
        "count": total,
        "truncated": total > len(rows),
        "entries": entries,
    }


def _read_event_chain(
    conn: Connection,
    doc_id: str,
    legacy_rows: list[dict[str, Any]],
    legacy_total: int,
    payload: dict[str, Any],
) -> None:
    total = _count(conn, "audit_log", "doc_id = :doc", {"doc": doc_id})
    payload["legacy"] = _legacy_projection(legacy_rows, legacy_total)
    if total == 0:
        payload["record"] = {"state": "legacy-events" if legacy_total else "not-recorded"}
        payload["verification"] = _legacy_only_verification(legacy_rows, legacy_total)
        return
    result = conn.execute(
        text(
            "SELECT * FROM audit_log WHERE doc_id = :doc "
            "ORDER BY seq, timestamp, entry_id LIMIT :limit"
        ),
        {"doc": doc_id, "limit": MAX_EVENTS + 1},
    )
    rows = [_row_dict(row) for row in result]
    truncated = len(rows) > MAX_EVENTS
    rows = rows[:MAX_EVENTS]
    events = [_project_entry(row, source="events") for row in rows]
    last = events[-1] if events else None
    payload["record"] = {
        "state": "event-chain",
        "entries": total,
        "last_event": last["event"] if last else None,
        "status": last["status"] if last else None,
        "started_at": events[0]["timestamp"] if events else None,
        "updated_at": last["timestamp"] if last else None,
        "events": events,
    }
    legacy_truncated = legacy_total > len(legacy_rows)
    legacy_failures: list[dict[str, Any]] = []
    if legacy_rows and not legacy_truncated:
        legacy_failures = _verify_legacy_chain(legacy_rows)
    if truncated or legacy_truncated:
        # A truncated legacy section is not verified at all, but a fully read
        # one still contributes its failures: truncation elsewhere only
        # prevents "verified".
        failures: list[dict[str, Any]] = legacy_failures
        verification = {
            "status": "failed" if failures else "unverified",
            "method": "schemas.audit.verify_chain",
            "checked": {"main": 0, "attempts": 0, "addenda": 0, "legacy": len(legacy_rows)},
            "failures": failures,
            "truncated": True,
            "notes": ["a bounded read was truncated; nothing is reported as verified"],
        }
    else:
        failures = _verify_legacy_chain(rows, scope="events")
        failures.extend(legacy_failures)
        verification = {
            "status": "verified" if not failures else "failed",
            "method": "schemas.audit.verify_chain",
            "checked": {"main": len(rows), "attempts": 0, "addenda": 0, "legacy": len(legacy_rows)},
            "failures": failures,
            "truncated": False,
            "notes": ["per-document event chain"],
        }
    payload["verification"] = verification


def _legacy_only_verification(legacy_rows: list[dict[str, Any]], legacy_total: int) -> dict[str, Any]:
    if not legacy_total:
        return {
            "status": "not-recorded",
            "method": None,
            "checked": {"main": 0, "attempts": 0, "addenda": 0, "legacy": 0},
            "failures": [],
            "truncated": False,
            "notes": ["no audit rows were found for this document id"],
        }
    truncated = legacy_total > len(legacy_rows)
    if truncated:
        return {
            "status": "unverified",
            "method": "schemas.audit.verify_chain",
            "checked": {"main": 0, "attempts": 0, "addenda": 0, "legacy": len(legacy_rows)},
            "failures": [],
            "truncated": True,
            "notes": ["the legacy chain exceeds the bounded read; it is not reported as verified"],
        }
    failures = _verify_legacy_chain(legacy_rows)
    return {
        "status": "verified" if not failures else "failed",
        "method": "schemas.audit.verify_chain",
        "checked": {"main": 0, "attempts": 0, "addenda": 0, "legacy": len(legacy_rows)},
        "failures": failures,
        "truncated": False,
        "notes": ["migrated legacy events; no unified terminal row for this document"],
    }


def _read_unified(
    conn: Connection,
    doc_id: str,
    tables: set[str],
    legacy_rows: list[dict[str, Any]],
    legacy_total: int,
    payload: dict[str, Any],
) -> None:
    payload["legacy"] = _legacy_projection(legacy_rows, legacy_total)
    # A marker-only store may have no unique ``doc_id`` index, so more than one
    # terminal row is possible. Read two and refuse to interpret either: picking
    # the first would present an arbitrary row as the document's record.
    rows = conn.execute(
        text("SELECT * FROM audit_log WHERE doc_id = :doc ORDER BY seq LIMIT 2"),
        {"doc": doc_id},
    ).fetchall()
    if not rows:
        payload["record"] = {"state": "legacy-events" if legacy_total else "not-recorded"}
        verification = _legacy_only_verification(legacy_rows, legacy_total)
        orphan_attempts = (
            _count(conn, "audit_attempts", "doc_id = :doc", {"doc": doc_id})
            if "audit_attempts" in tables
            else 0
        )
        orphan_addenda = (
            _count(conn, "audit_addenda", "doc_id = :doc", {"doc": doc_id})
            if "audit_addenda" in tables
            else 0
        )
        if orphan_attempts or orphan_addenda:
            # A verified legacy section cannot account for sub-rows whose
            # terminal row is gone: they are orphans, not evidence.
            for scope, count, reason in (
                ("attempts", orphan_attempts, "attempt-orphan"),
                ("addenda", orphan_addenda, "addendum-orphan"),
            ):
                if count:
                    verification["failures"].append(
                        {"scope": scope, "id": None, "reason": reason, "count": count}
                    )
            verification["status"] = "failed"
            verification["notes"].append(
                "sub-rows exist without a terminal row; the legacy section alone cannot account for them"
            )
        payload["verification"] = verification
        return
    if len(rows) > 1:
        hash_entry, _ = _foundation_writer()
        payload["record"] = {
            "state": "duplicate-row",
            "reason": "duplicate-terminal-row",
            "entries": _count(conn, "audit_log", "doc_id = :doc", {"doc": doc_id}),
        }
        payload["verification"] = {
            "status": "failed",
            "method": "audit.writer" if hash_entry is not None else "schemas.audit",
            "checked": {"main": 0, "attempts": 0, "addenda": 0, "legacy": 0},
            "failures": [
                {"scope": "main", "id": None, "reason": "duplicate-terminal-row"}
            ],
            "truncated": False,
            "notes": [
                "more than one terminal audit row exists for this document; no row is "
                "treated as its record and the store is not reported as verified"
            ],
        }
        payload["notes"].append(
            "duplicate terminal rows: the unified schema must hold one row per document"
        )
        return
    data = _row_dict(rows[0])
    # Matter id is the chain's shared context: the row supplies it for addenda
    # and attempt hashes (foundation writer contract). Keep it raw here; the
    # projection below trims it for display only.
    matter_id = data.get("matter_id")
    seq = _as_int(data.get("seq"))
    predecessor = None
    if seq is not None:
        found = conn.execute(
            text(
                "SELECT entry_id, entry_hash, seq FROM audit_log "
                "WHERE seq < :seq ORDER BY seq DESC LIMIT 1"
            ),
            {"seq": seq},
        ).first()
        predecessor = _row_dict(found) if found is not None else None

    # Instrumentation point: a regression test mutates the database here (after
    # the main row was read) and proves the open snapshot still governs the
    # addenda/attempt reads.
    _notify("mid", conn)

    attempts_total = 0
    attempts: list[dict[str, Any]] = []
    addenda: list[dict[str, Any]] = []
    truncated: list[str] = []
    if "audit_attempts" in tables:
        attempts_total = _count(conn, "audit_attempts", "doc_id = :doc", {"doc": doc_id})
        result = conn.execute(
            text(
                "SELECT * FROM audit_attempts WHERE doc_id = :doc "
                "ORDER BY stage, attempt_no, attempt_id LIMIT :limit"
            ),
            {"doc": doc_id, "limit": MAX_ATTEMPTS + 1},
        )
        attempts = [_row_dict(item) for item in result]
        if len(attempts) > MAX_ATTEMPTS or attempts_total > MAX_ATTEMPTS:
            truncated.append("attempts")
            attempts = attempts[:MAX_ATTEMPTS]
    if "audit_addenda" in tables:
        addenda_total = _count(conn, "audit_addenda", "doc_id = :doc", {"doc": doc_id})
        result = conn.execute(
            text(
                "SELECT * FROM audit_addenda WHERE doc_id = :doc ORDER BY seq LIMIT :limit"
            ),
            {"doc": doc_id, "limit": MAX_ADDENDA + 1},
        )
        addenda = [_row_dict(item) for item in result if item is not None]
        if len(addenda) > MAX_ADDENDA or addenda_total > MAX_ADDENDA:
            truncated.append("addenda")
            addenda = addenda[:MAX_ADDENDA]

    attempt_projection = [_project_attempt(item) for item in attempts]
    parent_of = {_as_str(item.get("attempt_id")): _as_str(item.get("parent_id")) for item in attempts}
    row_id = _as_str(data.get("entry_id"))
    event = _as_str(data.get("event"), limit=64)
    payload["record"] = {
        "state": "unified-row",
        "entry_id": row_id,
        "matter_id": _as_str(data.get("matter_id")),
        "event": event,
        "actor": _as_str(data.get("actor"), limit=64),
        "timestamp": _iso(data.get("timestamp")),
        "seq": seq,
        "prev_hash": _hash(data.get("prev_hash")),
        "entry_hash": _hash(data.get("entry_hash")),
        "status": _TERMINAL_STATUS.get(event or "", "recorded"),
        "terminal": event in _TERMINAL_STATUS,
        "decision_fields": _decision_fields(data.get("detail")),
        "attempts": [item for item in attempt_projection if parent_of.get(item["attempt_id"]) == row_id],
        "attempts_total": len([item for item in attempts if _as_str(item.get("parent_id")) == row_id]),
        "addenda": [],
    }
    for addendum in addenda:
        addendum_id = _as_str(addendum.get("addendum_id"))
        payload["record"]["addenda"].append(
            _project_addendum(
                addendum,
                [item for item in attempt_projection if parent_of.get(item["attempt_id"]) == addendum_id],
            )
        )
    payload["verification"] = _verify_unified(
        row=data,
        predecessor=predecessor,
        attempts=attempts,
        addenda=addenda,
        legacy=legacy_rows,
        truncated=truncated,
        matter_id=matter_id,
        attempts_available="audit_attempts" in tables,
    )
    if legacy_total > len(legacy_rows):
        payload["verification"]["truncated"] = True
        payload["verification"]["notes"].append("legacy section exceeds the bounded read")
        if payload["verification"]["status"] != "failed":
            # A detected mismatch stays failed; truncation alone only prevents
            # "verified".
            payload["verification"]["status"] = "unverified"
    if payload["verification"]["status"] == "failed":
        payload["notes"].append(
            "hash verification failed: treat the record as tampered or inconsistent"
        )

