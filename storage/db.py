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
BASE_DIR = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")).resolve()
DEFAULT_DB_URL = f"sqlite+aiosqlite:///{BASE_DIR / 'mailroom.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_DB_URL

engine_kwargs = {"echo": False}
if DATABASE_URL.startswith("sqlite"):
    # NullPool: one fresh connection per session. aiosqlite connections are
    # tied to the event loop that created them, so pooling across loops (the
    # graph runs sync nodes that spawn asyncio.run()/threadsafe coroutines)
    # would break. A fresh connection per session avoids cross-loop reuse.
    engine_kwargs["poolclass"] = NullPool
    try:
        Path(DATABASE_URL.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("sqlite_dir_ensure_failed", url=DATABASE_URL)

engine = create_async_engine(DATABASE_URL, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _ensure_models_imported():
    # Register every table on Base.metadata. Imported lazily (and inside
    # function bodies only) so this never creates a circular import —
    # storage.catalog / storage.audit_log both import from storage.db.
    import storage.catalog  # noqa: F401
    import storage.audit_log  # noqa: F401


_schema_checked = False


def ensure_schema() -> bool:
    """Create all tables if they don't exist yet. Thread-safe, idempotent.

    Call before any read/write so a fresh install works with zero setup.
    """
    global _schema_checked
    if _schema_checked:
        return True
    _ensure_models_imported()
    try:
        if DATABASE_URL.startswith("sqlite"):
            from sqlalchemy import create_engine

            # Sync sqlite driver (stdlib) — no event loop involvement, so this
            # is safe to call from graph nodes, watcher threads, or the API.
            sync_url = DATABASE_URL.replace("+aiosqlite", "")
            sync_engine = create_engine(sync_url)
            Base.metadata.create_all(sync_engine)  # checkfirst=True by default
            sync_engine.dispose()
        else:
            # Postgres: needs an async loop. Only safe outside a running loop.
            asyncio.run(init_db())
        _schema_checked = True
        logger.info("schema_ready", url=DATABASE_URL)
        return True
    except Exception:
        logger.exception("schema_creation_failed")
        return False


async def init_db():
    _ensure_models_imported()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_initialized", url=DATABASE_URL)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


async def close_db():
    await engine.dispose()
    logger.info("database_disposed")
