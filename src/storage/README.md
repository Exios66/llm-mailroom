# `storage/` — The database layer

## What this folder is (plain English)

Everything Mailroom remembers lives here. It uses **SQLite** — a single file database, no server to install or configure. You never have to start a database or run setup commands; the first time something needs the DB, it creates the file and tables automatically.

Two things are stored:

1. **Catalog** (`catalog.py`) — the document registry: every document and its stage, type, confidence scores, and extracted data, plus `matters` (client matters).
2. **Audit log** (`audit_log.py`) — an append-only log of every pipeline event (classified, extracted, archived, …), hash-chained so tampering is detectable.

Where the files live (under `MAILROOM_BASE_DIR`, default `data/`):

- `data/mailroom.db` — the SQLite database (tables: `matters`, `documents`, `audit_log`)
- `data/checkpoints.db` — LangGraph crash-resume state (managed by `graph/build_graph.py`)

**You can inspect everything with a simple SQL tool** (e.g. `sqlite3 data/mailroom.db ".tables"` and `"SELECT * FROM audit_log;"`), or via the API (`/status`, `/audit`, `/matters`).

## Technical reference

- `db.py`
  - Builds the SQLAlchemy async engine from `DATABASE_URL`. Default is SQLite at `{MAILROOM_BASE_DIR}/mailroom.db`. To use Postgres instead, set `DATABASE_URL` (e.g. `postgresql+asyncpg://mailroom:mailroom@localhost:5432/mailroom`).
  - Uses `NullPool` for SQLite: one fresh connection per session, because aiosqlite connections are tied to the event loop that created them and the graph spawns loops from sync threads (`asyncio.run` / `run_coroutine_threadsafe`).
  - `Base` — the declarative base all tables inherit from.
  - `ensure_schema()` — idempotent, thread-safe table creation (creates tables on first use, so a fresh install needs zero setup). Uses a sync `sqlite3` engine so it works from graph nodes, watcher threads, and the API alike.
  - `init_db()` — async table creation (for Postgres or explicit setup).
  - `get_session()`, `close_db()`.
- `catalog.py` — ORM models `MatterRecord` (table `matters`), `DocumentRecord` (table `documents`, JSON column for `extracted_data`). Functions: `write_matter_record`, `write_document_record`, `get_document`, `get_matter_documents`, `get_stuck_documents`, `get_documents_by_stage`, `get_error_rate_by_doc_type`. All call `ensure_schema()` first.
- `audit_log.py` — ORM model `AuditLogRecord` (table `audit_log`). Functions: `write_audit_entry(entry)`, `get_audit_chain(doc_id)` (returns dicts ordered by timestamp), `get_latest_audit_hash(doc_id)`. Hash values are produced by `schemas/audit.py`, not here.
- Graph writes to the catalog/audit are **best-effort**: `graph/build_graph.py` wraps them in `try/except` and logs on failure — the pipeline continues even if the DB is broken.
- If you ever switch to Postgres: start `docker compose -f config/docker/docker-compose.yml up -d postgres`, set `DATABASE_URL`, and the tables auto-create on first use (or run `python -c "import asyncio; from storage.db import init_db; asyncio.run(init_db())"`).
