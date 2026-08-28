# Local storage alternatives for audit and review

- **Date**: 2026-08-27
- **Kind**: audits (Repository audit and synthesis reports)
- **Status**: complete
- **Related**: The-Mailroom [PR #18](https://github.com/Exios66/The-Mailroom/pull/18) (REVIEW resolve tray); producer `GET /lookup`, disposition-aware `POST /review/{doc_id}/resolve`, `GET /audit` analyze

## Scope

Evaluate the current SQLite catalog + hash-chained audit log (`data/mailroom.db`)
against local alternatives for storing finished matters/documents so operators
can audit pipeline runs and drive human review without a cloud database.

Current surface (keep as baseline):

| Store | Role | Access |
|---|---|---|
| `mailroom.db` SQLite | `matters`, `documents`, `audit_log` | SQLAlchemy async + aiosqlite; WAL + FK + busy_timeout |
| `checkpoints.db` | Optional LangGraph SqliteSaver | Off by default (`MemorySaver`) |
| Filesystem bins | inbox / processing / review / failed / archive | Source of truth for *files* |
| Manifests JSON | Per-doc stage + classification | Resume / review resolve |

## Method

Compared candidates on: zero-ops local install, concurrent writer safety with
the watcher + API, audit-chain integrity, ad-hoc analytics for human review,
migration cost from today's ORM, and fit for "finished matters" cold storage.

## Findings

1. **SQLite (status quo) remains the right default for live ops.** Single file,
   no server, already hardened (WAL, busy_timeout 5s, FK on audit→documents).
   At mailroom volumes (tens–hundreds of docs/day, not millions) it is not the
   bottleneck. The new `scripts/analyze_audit_db.py` / `GET /audit` cover the
   "parse the whole local audit DB" gap without changing engines.

2. **DuckDB is the strongest *analytics* companion, not a swap-in OLTP store.**
   Columnar, excellent for scanning `audit_log` + joining catalog for review
   histograms, pilot diffs, and RECONSIDER audits. Can `ATTACH` the live
   SQLite file read-only or ingest nightly Parquet dumps. Do **not** make
   DuckDB the writer for hash-chained appends (different concurrency model;
   weaker fit for per-request upserts from graph nodes).

3. **SQLite + Parquet export is the best efficiency win for finished matters.**
   Keep hot rows (inbox/processing/review) in SQLite; periodically export
   `stage IN ('archived','failed')` documents + their audit chains to
   `data/warehouse/matters_YYYYMMDD.parquet` (PyArrow already in the env via
   Phoenix/deps). Audits and The-Mailroom retrospective trays read Parquet;
   live resolve still hits SQLite. Cheap, portable, git-annex / object-store
   friendly.

4. **LiteFS / Litestream add durability, not query power.** Streaming SQLite
   replication to S3 (Litestream) or a FUSE primary (LiteFS) protects the
   compliance audit file. Worth it when the DB is the compliance system of
   record and the host is ephemeral — orthogonal to DuckDB/Parquet.

5. **Postgres remains the only justified *server* upgrade.** Already supported
   via `DATABASE_URL`. Choose it when multiple writer hosts share one catalog,
   or when org policy requires a managed RDBMS. Heavier than needed for a
   single-operator laptop/pilot box.

6. **Avoid as primary local stores for this workload:**
   - **JSONL / append-only files alone** — lose indexed lookup by `trace_id` /
     `doc_id` that REVIEW resolve needs (`GET /lookup`).
   - **Mongo / embedded document DBs** — no win over SQLite JSON columns; worse
     ops story.
   - **Replacing the hash chain with a Merkle tree in object storage** —
     interesting for WORM compliance archives, but does not help the live
     review tray; keep as a future cold-archive export, not the working set.

## Recommendations

| Priority | Action |
|---|---|
| Now (shipped) | Keep SQLite; use `analyze_audit_db.py` / `GET /audit` + REVIEW dispositions |
| Now (shipped) | Parquet warehouse: `storage/warehouse.py` + `scripts/export_warehouse.py`; routine hook on archive/failed |
| Optional | Read-only DuckDB notebook/CLI that `ATTACH`es `mailroom.db` or reads `data/warehouse/*.parquet` |
| When multi-host | Switch `DATABASE_URL` to Postgres; keep the same ORM models |
| When compliance host is ephemeral | Add Litestream (or equivalent) backup of `mailroom.db` |

### Suggested Parquet layout (implemented)

```
data/warehouse/
  documents_2026-08-27.parquet   # finished matters/docs (archived + failed)
  audit_2026-08-27.parquet       # matching audit_log rows
  manifest.json                  # export watermark + schema version
```

Routine export runs after archive/failed (`MAILROOM_WAREHOUSE_EXPORT=auto|1|0`).
Backfill: `PYTHONPATH=src python src/scripts/export_warehouse.py [--full]`.

Join key: `doc_id`. The-Mailroom can later point a "cold review" tray at the
warehouse without touching the live writer.

### Decision

**Do not replace SQLite for the live catalog/audit.** Add analytics *beside*
it (analyze CLI now; Parquet/DuckDB later). That maximizes review-tray
reliability (lookups, resolve, requeue) while making full-history audits cheap.
