"""One-time migration of the per-event ``audit_log`` to the unified audit row.

Spec ``2026-11-03-unified-audit-row-design.md`` §4.5 and §6, roadmap task M2.

Before: ``audit_log`` holds many hash-chained rows per document, ``seq``
counted per document. After: ``audit_log`` holds one row per document (unique
``doc_id``, one global ``seq``), and the old rows live on unchanged in
``audit_log_legacy``.

``migrate_audit_log`` runs under ``acquire_write_lock(session,
"audit_log:chain")``, in one transaction:

1. ``audit_meta`` already has the ``migration`` key: do nothing.
2. Postgres with rows to move runs only with ``MAILROOM_AUDIT_MIGRATE=1``
   (the operator takes a ``pg_dump`` first); otherwise log
   ``audit_migration_pending`` and leave everything as it is.
3. SQLite: back the database up (``<db dir>/backups/`` by default).
4. Copy every ``audit_log`` row into ``audit_log_legacy`` with one
   ``INSERT ... SELECT``, so every column keeps its stored value.
5. Compare counts and a digest of every column; any difference aborts the
   transaction before ``audit_log`` is emptied.
6. Empty ``audit_log``, put the unique ``doc_id`` and ``seq`` indexes on it,
   write ``audit_meta``.

``rollback_audit_migration`` (``python -m storage.audit_migrate --rollback``)
renames the new tables to ``*_v2_<timestamp>`` and restores ``audit_log`` as the
previous release created it, from ``audit_log_legacy``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    inspect,
    text,
)
from sqlalchemy.schema import CreateIndex, CreateTable

from . import db
from .audit_log import (
    AuditLegacyRecord,
    AuditLogRecord,
    AuditMetaRecord,
)
from .db import acquire_write_lock, async_session

logger = structlog.get_logger(__name__)

LOCK_KEY = "audit_log:chain"
META_KEY = "migration"
OPT_IN_ENV = "MAILROOM_AUDIT_MIGRATE"

#: Tables the unified audit row adds or reshapes; the rollback renames them.
V2_TABLES = ("audit_log", "audit_attempts", "audit_addenda", "audit_meta", "audit_trace_io")
LEGACY_TABLE = "audit_log_legacy"

_COLUMNS = "entry_id, doc_id, matter_id, event, actor, detail, prev_hash, entry_hash, seq, \"timestamp\""
_FINGERPRINT_COLUMNS = (
    "entry_id, doc_id, matter_id, event, actor, CAST(detail AS TEXT), prev_hash, entry_hash, seq, "
    "CAST(\"timestamp\" AS TEXT)"
)

_PENDING_HINT = (
    "audit_log still holds the previous release's rows. Back up the database (pg_dump), then "
    f"restart with {OPT_IN_ENV}=1 or run `{OPT_IN_ENV}=1 python -m storage.audit_migrate`."
)


class AuditMigrationError(RuntimeError):
    """The migration found something it must not paper over; nothing was changed."""


@dataclass
class MigrationResult:
    migrated: bool
    legacy_rows: int
    backup_path: str | None = None
    #: why nothing was migrated: already_migrated | no_legacy_rows | opt_in_required
    reason: str | None = None


@dataclass
class RollbackResult:
    rolled_back: bool
    legacy_rows: int
    #: live table name -> the name it was renamed to
    renamed: dict[str, str] = field(default_factory=dict)


@dataclass
class LegacyChainReport:
    ok: bool
    checked: int
    failures: list[dict] = field(default_factory=list)


# --- DDL --------------------------------------------------------------------------------
#
# Everything the migration and the rollback execute besides row copies is built
# here from SQLAlchemy constructs, so one function renders it for the live
# dialect and the tests render it for ``postgresql``.

_legacy_meta = MetaData()
# The referenced table only has to exist for DDL compilation of the foreign key.
Table("documents", _legacy_meta, Column("doc_id", String(128), primary_key=True))
#: ``audit_log`` exactly as the previous release created it (per-document ``seq``,
#: plain ``doc_id`` index), which the rollback restores.
LEGACY_AUDIT_LOG = Table(
    "audit_log",
    _legacy_meta,
    Column("entry_id", String(128), primary_key=True),
    Column("doc_id", String(128), ForeignKey("documents.doc_id", ondelete="RESTRICT"), nullable=False),
    Column("matter_id", String(128), nullable=False),
    Column("event", String(128), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("prev_hash", String(256), nullable=False),
    Column("entry_hash", String(256), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Index("ix_audit_log_matter_id", "matter_id"),
    Index("ix_audit_log_doc_id", "doc_id"),
)


def _render(construct, dialect) -> str:
    return str(construct.compile(dialect=dialect)).strip()


def _model_tables() -> dict:
    db._ensure_models_imported()
    return db.Base.metadata.tables


def _model_index_names(table: str) -> list[str]:
    return sorted(ix.name for ix in _model_tables()[table].indexes)


def migration_ddl(dialect) -> list[str]:
    """Statements that turn an emptied legacy-shape ``audit_log`` into the unified one."""
    indexes = [ix for ix in _model_tables()["audit_log"].indexes if ix.unique]
    statements = [f"DROP INDEX IF EXISTS {ix.name}" for ix in sorted(indexes, key=lambda i: i.name)]
    statements += [_render(CreateIndex(ix), dialect) for ix in sorted(indexes, key=lambda i: i.name)]
    return statements


def _rollback_phases(dialect, stamp: str) -> tuple[list[str], list[str]]:
    """(before the copy back, after it). ``audit_log_legacy`` is renamed last,
    because the restored ``audit_log`` is copied from it."""
    before: list[str] = []
    for table in V2_TABLES:
        before += [f"DROP INDEX IF EXISTS {name}" for name in _model_index_names(table)]
    before += [f"ALTER TABLE {table} RENAME TO {table}_v2_{stamp}" for table in V2_TABLES]
    before.append(_render(CreateTable(LEGACY_AUDIT_LOG), dialect))
    before += [_render(CreateIndex(ix), dialect) for ix in sorted(LEGACY_AUDIT_LOG.indexes, key=lambda i: i.name)]
    after = [f"DROP INDEX IF EXISTS {name}" for name in _model_index_names(LEGACY_TABLE)]
    after.append(f"ALTER TABLE {LEGACY_TABLE} RENAME TO {LEGACY_TABLE}_v2_{stamp}")
    return before, after


def rollback_ddl(dialect, stamp: str) -> list[str]:
    """Every DDL statement of the rollback, in order."""
    before, after = _rollback_phases(dialect, stamp)
    return before + after


# --- helpers ----------------------------------------------------------------------------


def _dialect_name(session) -> str:
    return session.get_bind().dialect.name


def _opted_in() -> bool:
    return os.environ.get(OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _scalar(session, sql: str):
    return (await session.execute(text(sql))).scalar()


async def _table_exists(session, name: str) -> bool:
    conn = await session.connection()
    return await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(name))


async def _is_unified_audit_log(session) -> bool:
    """Return whether ``audit_log`` has a unique doc-id index or constraint."""
    conn = await session.connection()

    def _check(sync_conn) -> bool:
        insp = inspect(sync_conn)
        indexes = insp.get_indexes("audit_log")
        constraints = insp.get_unique_constraints("audit_log")
        return any(i.get("unique") and i["column_names"] == ["doc_id"] for i in indexes) or any(
            c["column_names"] == ["doc_id"] for c in constraints
        )

    return await conn.run_sync(_check)


async def _migration_meta(session) -> dict | None:
    """Read and decode the migration marker, or return ``None`` if absent."""
    row = (
        await session.execute(
            text('SELECT "value" FROM audit_meta WHERE "key" = :key'), {"key": META_KEY}
        )
    ).first()
    if row is None:
        return None
    value = row[0]
    return json.loads(value) if isinstance(value, str) else dict(value)


async def _exec_all(session, statements: list[str]) -> None:
    for statement in statements:
        await session.execute(text(statement))


async def _fingerprint(session, table: str) -> tuple[int, str]:
    """(row count, digest over every column cast to text) of ``table``."""
    digest = hashlib.sha256()
    count = 0
    # Buffered execute, not session.stream(): an asyncpg server-side cursor
    # stays open in this transaction and makes the migration's later
    # CREATE INDEX on the same table fail ("being used by active queries").
    result = await session.execute(
        text(f"SELECT {_FINGERPRINT_COLUMNS} FROM {table} ORDER BY entry_id")
    )
    for row in result:
        digest.update(json.dumps(list(row), default=str).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


async def _copy_legacy_rows(session) -> None:
    """Copy every ``audit_log`` row into ``audit_log_legacy`` as stored."""
    await session.execute(
        text(f"INSERT INTO {LEGACY_TABLE} ({_COLUMNS}) SELECT {_COLUMNS} FROM audit_log")
    )


async def _verify_copy(session, source: str, copy: str) -> None:
    """Raise ``AuditMigrationError`` if table counts or column digests differ."""
    src = await _fingerprint(session, source)
    dst = await _fingerprint(session, copy)
    if src[0] != dst[0]:
        raise AuditMigrationError(
            f"{copy} has {dst[0]} rows but {source} has {src[0]}; nothing was changed"
        )
    if src[1] != dst[1]:
        raise AuditMigrationError(
            f"{copy} differs from {source} in at least one column (digest mismatch); nothing was changed"
        )


async def _tail_digest(session) -> str:
    """SHA-256 over each document's last legacy ``entry_hash`` (sorted by doc_id)."""
    tails: dict[str, str] = {}
    # Buffered for the same reason as _fingerprint (no open cursor).
    result = await session.execute(
        text(
            f"SELECT doc_id, entry_hash FROM {LEGACY_TABLE} "
            'ORDER BY doc_id, seq, "timestamp", entry_id'
        )
    )
    for doc_id, entry_hash in result:
        tails[doc_id] = entry_hash
    payload = "\n".join(f"{doc_id}\t{tails[doc_id]}" for doc_id in sorted(tails))
    return hashlib.sha256(payload.encode()).hexdigest()


def _backup_sqlite_file(src: Path, dest_dir: Path) -> Path:
    """Back up a consistent SQLite snapshot, including WAL, to a new path.

    Create ``dest_dir`` if needed and return the backup path. Filesystem and
    SQLite errors propagate.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"{src.stem}.pre-audit-migration-{stamp}{src.suffix}"
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{src.stem}.pre-audit-migration-{stamp}-{n}{src.suffix}"
    source = sqlite3.connect(src, timeout=30)
    target = sqlite3.connect(dest)
    # Owner-only from the first byte: a failed backup must not leave a
    # world-readable partial database (PR #220 review).
    os.chmod(dest, 0o600)
    try:
        # The online backup API reads a consistent snapshot, WAL included.
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


async def _backup_sqlite(session, backup_dir: Path | None) -> str | None:
    """Return the SQLite backup path, or ``None`` for an in-memory database.

    Use ``backup_dir`` when supplied, otherwise ``<db dir>/backups/``.
    Backup errors propagate.
    """
    database = session.get_bind().url.database
    if not database or database == ":memory:":
        logger.warning("audit_migration_backup_skipped", reason="in-memory database")
        return None
    src = Path(database)
    dest_dir = Path(backup_dir) if backup_dir is not None else src.parent / "backups"
    dest = await asyncio.to_thread(_backup_sqlite_file, src, dest_dir)
    return str(dest)


async def _write_meta(session, *, legacy_rows: int, tail_digest: str) -> None:
    session.add(
        AuditMetaRecord(
            key=META_KEY,
            value={
                "migrated_at": _utc_now().isoformat(),
                "legacy_rows": legacy_rows,
                "legacy_tail_hashes_sha256": tail_digest,
            },
        )
    )
    await session.flush()


# --- migration --------------------------------------------------------------------------


async def _migrate_locked(session, backup_dir: Path | None) -> MigrationResult:
    """Migrate under the caller's chain lock without committing the transaction.

    Return a reason when already migrated, empty, or awaiting Postgres opt-in.
    Raise ``AuditMigrationError`` for ambiguous existing data or a copy mismatch;
    backup and database errors propagate.
    """
    dialect = _dialect_name(session)
    meta = await _migration_meta(session)
    if meta is not None:
        return MigrationResult(False, int(meta.get("legacy_rows", 0)), None, "already_migrated")

    rows = await _scalar(session, "SELECT count(*) FROM audit_log")
    if await _scalar(session, f"SELECT count(*) FROM {LEGACY_TABLE}"):
        raise AuditMigrationError(
            f"{LEGACY_TABLE} already holds rows but audit_meta has no migration record; "
            "refusing to copy over them. Inspect the database or run --rollback."
        )
    unified = await _is_unified_audit_log(session)

    if rows == 0:
        # Fresh install, or an old-shape table with nothing in it: no data to move.
        if not unified:
            await _exec_all(session, migration_ddl(session.get_bind().dialect))
        await _write_meta(session, legacy_rows=0, tail_digest=hashlib.sha256(b"").hexdigest())
        return MigrationResult(False, 0, None, "no_legacy_rows")
    if unified:
        raise AuditMigrationError(
            "audit_log has rows and already has the unified shape, but audit_meta has no "
            "migration record; refusing to guess which rows are legacy."
        )

    if dialect == "postgresql" and not _opted_in():
        logger.warning("audit_migration_pending", legacy_rows=rows, hint=_PENDING_HINT)
        return MigrationResult(False, rows, None, "opt_in_required")

    backup_path = await _backup_sqlite(session, backup_dir) if dialect == "sqlite" else None
    await _copy_legacy_rows(session)
    await _verify_copy(session, "audit_log", LEGACY_TABLE)
    tail_digest = await _tail_digest(session)
    await session.execute(text("DELETE FROM audit_log"))
    await _exec_all(session, migration_ddl(session.get_bind().dialect))
    await _write_meta(session, legacy_rows=rows, tail_digest=tail_digest)
    return MigrationResult(True, rows, backup_path)


async def _run_migration(backup_dir: Path | None) -> MigrationResult:
    """Run and commit the migration under the chain lock; roll back and re-raise errors."""
    async with async_session() as session:
        await acquire_write_lock(session, LOCK_KEY)
        try:
            result = await _migrate_locked(session, backup_dir)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
    if result.migrated:
        logger.info(
            "audit_migration_done", legacy_rows=result.legacy_rows, backup_path=result.backup_path
        )
    return result


async def migrate_at_startup() -> MigrationResult:
    """Run the migration with existing tables and the default SQLite backup directory.

    Return the same outcomes as ``migrate_audit_log``. Errors propagate to
    ``ensure_schema``'s migration wrapper, which catches ordinary exceptions.
    """
    return await _run_migration(None)


async def migrate_audit_log(*, backup_dir: Path | None = None) -> MigrationResult:
    """Migrate ``audit_log`` to the unified shape once; safe to call again.

    Creates any missing table first. ``backup_dir`` is where the SQLite backup
    goes (default ``<db dir>/backups/``); in-memory databases have no backup.
    Postgres with legacy rows requires ``MAILROOM_AUDIT_MIGRATE`` set to
    ``1``, ``true``, ``yes``, or ``on`` (case-insensitive); take a backup first.

    Return a ``MigrationResult`` with the legacy row count and backup path.
    ``migrated=False`` means already migrated, no legacy rows (the migration
    marker is still written), or Postgres opt-in required; ``reason`` identifies
    which. Raise :class:`AuditMigrationError` for ambiguous existing data or a
    copy mismatch. Backup and database errors propagate. The migration
    transaction is rolled back on failure; created tables and backup files
    may remain.
    """
    await asyncio.to_thread(db.ensure_tables)
    return await _run_migration(backup_dir)


# --- rollback ---------------------------------------------------------------------------


async def rollback_audit_migration() -> RollbackResult:
    """Undo the migration so the previous release runs again.

    The unified tables are renamed ``*_v2_<timestamp>`` (rows written since the
    migration stay in them, so a rollback loses nothing), ``audit_log`` is
    recreated as the previous release had it and refilled from
    ``audit_log_legacy``, which is renamed last. Does nothing when no migration
    is recorded.

    Return the restored legacy row count and the table rename mapping.
    Raise ``AuditMigrationError`` if the legacy table is missing, a rename
    target exists, or the restored copy differs. Database errors propagate;
    failed transactions are rolled back. Success clears the schema cache,
    so a later ``ensure_schema`` call can migrate again.
    """
    async with async_session() as session:
        await acquire_write_lock(session, LOCK_KEY)
        try:
            result = await _rollback_locked(session)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
    if result.rolled_back:
        # A later ensure_schema() in this process must rebuild and re-migrate.
        db._schema_checked_url = None
        logger.info("audit_migration_rolled_back", legacy_rows=result.legacy_rows, renamed=result.renamed)
    return result


async def _rollback_locked(session) -> RollbackResult:
    """Restore the legacy table under the caller's chain lock without committing."""
    if not await _table_exists(session, "audit_meta") or await _migration_meta(session) is None:
        return RollbackResult(False, 0, {})
    if not await _table_exists(session, LEGACY_TABLE):
        raise AuditMigrationError(f"audit_meta records a migration but {LEGACY_TABLE} is missing")
    dialect = session.get_bind().dialect
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    renamed = {table: f"{table}_v2_{stamp}" for table in (*V2_TABLES, LEGACY_TABLE)}
    for target in renamed.values():
        if await _table_exists(session, target):
            raise AuditMigrationError(f"{target} already exists; try again in a second")

    legacy_rows = await _scalar(session, f"SELECT count(*) FROM {LEGACY_TABLE}")
    before, after = _rollback_phases(dialect, stamp)
    await _exec_all(session, before)
    await session.execute(
        text(f"INSERT INTO audit_log ({_COLUMNS}) SELECT {_COLUMNS} FROM {LEGACY_TABLE}")
    )
    await _verify_copy(session, LEGACY_TABLE, "audit_log")
    await _exec_all(session, after)
    return RollbackResult(True, legacy_rows, renamed)


# --- verification (minimal; M4 extends it) -----------------------------------------------


async def verify_legacy_chain() -> LegacyChainReport:
    """Run today's per-document check on every document in ``audit_log_legacy``.

    Return overall validity, the number of documents checked, and each failed
    document's id and entry count. Verification orders entries by timestamp
    and entry id, checks ``prev_hash`` links, and recomputes v2 hashes (v1 for
    rows with both matter id and actor empty). Schema initialization may
    migrate legacy data; validation and database errors propagate.

    Roadmap M4 builds the full ``verify_audit_db`` around this and the sub-row,
    addendum and trace checks.
    """
    from schemas.audit import AuditLogEntry, verify_chain

    db.ensure_schema()

    def _utc(value: datetime) -> datetime:
        # SQLite stores DateTime(timezone=True) as naive UTC; the v2 hash covers
        # the isoformat, so re-attach the tz exactly as get_audit_chain does.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    async with async_session() as session:
        result = await session.execute(
            AuditLegacyRecord.__table__.select().order_by(
                AuditLegacyRecord.doc_id, AuditLegacyRecord.seq, AuditLegacyRecord.timestamp
            )
        )
        rows = result.all()
    by_doc: dict[str, list] = {}
    for row in rows:
        by_doc.setdefault(row.doc_id, []).append(row)
    failures = []
    for doc_id, chain in by_doc.items():
        entries = [
            AuditLogEntry(
                entry_id=r.entry_id,
                doc_id=doc_id,
                matter_id=r.matter_id or "",
                event=r.event,
                actor=r.actor,
                detail=r.detail,
                prev_hash=r.prev_hash,
                entry_hash=r.entry_hash,
                timestamp=_utc(r.timestamp),
            )
            for r in chain
        ]
        if not verify_chain(entries):
            failures.append({"doc_id": doc_id, "entries": len(chain)})
    return LegacyChainReport(ok=not failures, checked=len(by_doc), failures=failures)


# --- CLI --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Load the environment, migrate or roll back, and print the result as JSON.

    ``argv=None`` reads process arguments. Return 2 when Postgres opt-in is
    required, otherwise 0; migration and rollback errors propagate.
    """
    parser = argparse.ArgumentParser(
        prog="python -m storage.audit_migrate",
        description="Migrate audit_log to the unified audit row, or undo it.",
    )
    parser.add_argument("--rollback", action="store_true", help="restore the previous release's audit_log")
    parser.add_argument("--backup-dir", type=Path, default=None, help="SQLite backup directory")
    args = parser.parse_args(argv)

    from pipeline.env import load_env

    load_env()

    if args.rollback:
        outcome = asyncio.run(rollback_audit_migration())
        print(json.dumps(asdict(outcome), indent=2))
        return 0
    outcome = asyncio.run(migrate_audit_log(backup_dir=args.backup_dir))
    print(json.dumps(asdict(outcome), indent=2))
    # 2: Postgres has rows to move and MAILROOM_AUDIT_MIGRATE=1 is not set.
    return 2 if outcome.reason == "opt_in_required" else 0


if __name__ == "__main__":
    sys.exit(main())
