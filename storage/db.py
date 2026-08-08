import asyncio
import os
import structlog
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

logger = structlog.get_logger(__name__)

# SQLite by default — no database server needed. The DB file lives inside
# MAILROOM_BASE_DIR (default ./data/mailroom.db).
# Set DATABASE_URL to a Postgres URL (e.g.
# postgresql+asyncpg://user:pass@host:5432/mailroom) to use Postgres instead.
# The URL is resolved lazily (per call) so MAILROOM_BASE_DIR changes are
# honored — important for tests and multi-worker setups where the env differs
# from the importing process's initial state.
logger = structlog.get_logger(__name__)


def _resolve_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()
    return f"sqlite+aiosqlite:///{base / 'mailroom.db'}"


def _engine_kwargs(url: str) -> dict:
    kwargs = {"echo": False}
    if url.startswith("sqlite"):
        # NullPool: one fresh connection per session. aiosqlite connections are
        # tied to the event loop that created them, so pooling across loops (the
        # graph runs sync nodes that spawn asyncio.run()/threadsafe coroutines)
        # would break. A fresh connection per session avoids cross-loop reuse.
        kwargs["poolclass"] = NullPool
        try:
            Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("sqlite_dir_ensure_failed", url=url)
    return kwargs


_engine = None
_engine_url: str | None = None
_sessionmaker = None


def get_engine():
    """Lazily-built async engine, keyed by the current resolved URL."""
    global _engine, _engine_url, _sessionmaker
    url = _resolve_url()
    if _engine is None or _engine_url != url:
        _engine = create_async_engine(url, **_engine_kwargs(url))
        _engine_url = url
        _sessionmaker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


def async_session():
    """Return a new AsyncSession bound to the current URL's engine.

    Replaces the module-level sessionmaker so the engine follows
    MAILROOM_BASE_DIR/DATABASE_URL changes made after import. Call sites stay
    identical (`async with async_session() as session`).
    """
    return _get_sessionmaker()()


def _get_sessionmaker():
    get_engine()
    return _sessionmaker


class Base(DeclarativeBase):
    pass


def _ensure_models_imported():
    # Register every table on Base.metadata. Imported lazily (and inside
    # function bodies only) so this never creates a circular import —
    # storage.catalog / storage.audit_log both import from storage.db.
    import storage.catalog  # noqa: F401
    import storage.audit_log  # noqa: F401


_schema_checked_url: str | None = None


def ensure_schema() -> bool:
    """Create all tables if they don't exist yet. Thread-safe, idempotent.

    Call before any read/write so a fresh install works with zero setup.
    Also migrates pre-existing SQLite databases: tables created by an older
    build are missing newer columns, so every known column is diffed against
    the live schema and added via ALTER TABLE (SQLite supports ADD COLUMN).
    The idempotency cache is keyed by the resolved DB URL so a change of
    MAILROOM_BASE_DIR (e.g. per-test temp dirs) creates the schema in the
    right database.
    """
    global _schema_checked_url
    url = _resolve_url()
    if _schema_checked_url == url:
        return True
    _ensure_models_imported()
    try:
        if url.startswith("sqlite"):
            from sqlalchemy import create_engine

            # Sync sqlite driver (stdlib) — no event loop involvement, so this
            # is safe to call from graph nodes, watcher threads, or the API.
            sync_url = url.replace("+aiosqlite", "")
            sync_engine = create_engine(sync_url)
            Base.metadata.create_all(sync_engine)  # checkfirst=True by default
            _migrate_sqlite_columns(sync_engine)
            sync_engine.dispose()
        else:
            # Postgres: needs an async loop. Only safe outside a running loop.
            asyncio.run(init_db())
        _schema_checked_url = url
        logger.info("schema_ready", url=url)
        return True
    except Exception:
        logger.exception("schema_creation_failed")
        return False


def _migrate_sqlite_columns(sync_engine) -> None:
    """Add columns that newer code expects but pre-existing databases lack.

    SQLite's ALTER TABLE ADD COLUMN only permits nullable columns (or columns
    with a default), which every model column here is; JSON columns are stored
    with JSON affinity, matching how create_all builds fresh tables.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    inspector = inspect(sync_engine)
    for table in Base.metadata.tables.values():
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in existing]
        if not missing:
            continue
        ddl_type = sqlite_dialect()
        for col in missing:
            stmt = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=ddl_type)}"
            with sync_engine.begin() as conn:
                conn.execute(text(stmt))
            logger.info("schema_column_added", table=table.name, column=col.name)


async def init_db():
    _ensure_models_imported()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_initialized", url=_resolve_url())


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


async def close_db():
    await get_engine().dispose()
    logger.info("database_disposed")
