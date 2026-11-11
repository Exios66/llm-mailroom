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
- `catalog.py` — ORM models `MatterRecord` (table `matters`), `DocumentRecord` (table `documents`, JSON column for `extracted_data`). Functions: `write_matter_record`, `write_document_record`, `get_document`, `lookup_document` (by doc_id/trace_id/filename), `get_matter_documents`, `get_stuck_documents`, `get_documents_by_stage`, `get_error_rate_by_doc_type`. All call `ensure_schema()` first.
- `audit_migrate.py` — one-time, **opt-in** migration of the per-event `audit_log` to the unified audit row (`migrate_audit_log`, `migrate_at_startup`, `rollback_audit_migration`, `python -m storage.audit_migrate [--rollback]`). Default startup runs it only with `MAILROOM_AUDIT_MIGRATE=1` (SQLite and Postgres alike); otherwise `ensure_schema()` logs `audit_migration_pending` and leaves the current per-event table and writer untouched. The explicit CLI needs no knob on **SQLite**; **Postgres always requires `MAILROOM_AUDIT_MIGRATE=1`** (quiesce writers and `pg_dump` first). The migration runs under the `audit_log:chain` lock and skips once `audit_meta` records it; the old rows are kept byte for byte in `audit_log_legacy`. SQLite is backed up first (`<db dir>/backups/`); take your own `pg_dump` for Postgres. `verify_legacy_chain()` checks the legacy chains.
- `audit_log.py` — ORM models: `AuditLogRecord` (`audit_log`), `AuditLegacyRecord`, `AuditAttemptRecord`, `AuditAddendumRecord`, `AuditMetaRecord`, `AuditTraceIORecord`. **On purpose, the ORM does not declare unique `doc_id`/`seq`:** a fresh, unmigrated database must keep the current per-event shape (many rows per document, per-document `seq`) so `create_all` plus the current writer work with the migration default-off. The explicit migration installs `ux_audit_log_doc_id` and `ux_audit_log_seq` after copying the rows out. Until the row writers land, `write_audit_entry` and the per-document readers go through `event_log_model()`, which points them at `audit_log_legacy` once the migration has run and at `audit_log` while it is pending. Functions: `write_audit_entry(entry)`, `get_audit_chain(doc_id)`, `get_latest_audit_hash(doc_id)`, `analyze_audit_db()` (full-DB summary for `GET /audit` / `scripts/analyze_audit_db.py`). Hash values are produced by `schemas/audit.py`, not here.
- `audit/verify.py` (package `src/audit/`) — `verify_audit_db()` checks the unified main chain, attempt sub-row hashes/membership, addendum chains, the legacy per-document chain, and stored `audit_trace_io` hashes in one read-only snapshot. Pending rows/sinks are reported as **uncollected** (roadmap M9/M10). `scripts/verify_audit_chains.py` exposes it (`--json`, `--check-sinks`).
- `warehouse.py` — Parquet cold store for terminal documents + audit rows under `data/warehouse/` (`documents_YYYY-MM-DD.parquet`, `audit_YYYY-MM-DD.parquet`, `manifest.json`). Routine export via `export_document_to_warehouse()` after archive/failed; backfill via `scripts/export_warehouse.py`. Env: `MAILROOM_WAREHOUSE_EXPORT=auto|1|0`.
### Migrating the audit storage (opt-in, deliberate)

Default startup never migrates. The default keeps the per-event `audit_log` and its current writer. New tables may be created empty; no marker and no unique `doc_id`/`seq` indexes exist until you run the explicit step.

```bash
# 1. Quiesce writers (watcher/API) so no old process inserts during the copy.
# 2. Back up: SQLite gets an automatic 0600 copy under <db dir>/backups/;
#    Postgres needs your own pg_dump.
# 3. Migrate deliberately. SQLite needs no knob for the CLI; Postgres always
#    requires MAILROOM_AUDIT_MIGRATE=1 (quiesce writers + pg_dump first):
PYTHONPATH=src python -m storage.audit_migrate
# or opt the startup path in once (SQLite and Postgres):
MAILROOM_AUDIT_MIGRATE=1 PYTHONPATH=src python -m api.main
# 4. Verify:
PYTHONPATH=src python src/scripts/verify_audit_chains.py --json
```

The migration runs under the `audit_log:chain` lock in one transaction, copies the rows byte-for-byte into `audit_log_legacy`, checks counts and a column digest, empties `audit_log`, installs `ux_audit_log_doc_id`/`ux_audit_log_seq`, and records `audit_meta`. On Postgres it additionally takes `LOCK TABLE audit_log IN ACCESS EXCLUSIVE MODE` for the copy+delete window, because older releases do not take the advisory gate — hence the quiesce step. `--rollback` renames the new tables and restores the previous shape. After the migration the current per-event writer and readers keep working through `event_log_model()` (they append to/read `audit_log_legacy`).

`verify_audit_db()` checks what this layer can actually observe (main chain, sub-rows, addenda, legacy, stored trace I/O) in one read-only snapshot. This is **scope-bounded, not memory-bounded**: the offline verifier materializes those tables in memory, so cost grows with the rows checked. Pending manifests and sink pending records belong to later roadmap tasks and are reported as **not collected**, not as proof.

- Local storage alternatives (SQLite vs DuckDB/Parquet/Postgres/Litestream): `docs/reports/audits/2026-08-27-local-storage-alternatives-for-audit-and-review.md`.
- Graph writes to the catalog/audit are **best-effort**: `graph/build_graph.py` wraps them in `try/except` and logs on failure — the pipeline continues even if the DB is broken.
- If you ever switch to Postgres: start `docker compose -f config/docker/docker-compose.yml up -d postgres`, set `DATABASE_URL`, and the tables auto-create on first use (or run `python -c "import asyncio; from storage.db import init_db; asyncio.run(init_db())"`).
