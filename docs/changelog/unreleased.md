---
tags:
  - feature
---

# Unreleased

{% hint style="info" %}
This GitBook Changelog space is generated from the repository
[`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (Keep a Changelog). Do not hand-edit these
pages. Regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
{% endhint %}

Unreleased work on `main`. Canonical source: [`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md).

## Added

- **Native pipeline diagnostics foundation** under `tools/pipeline-diagnostics/`: an installable `mailroom-diagnostics` inspection/check CLI, compiled-graph snapshots through the display exporter's shared graph helper, offline contract input, structural endpoint/reachability findings, and nested test/CI discovery. The draft workbench plan includes an ordered agent handoff and evidence contract. React, diagnostic HTTP serving, live events and calibration remain later milestones; this checkpoint changes no routing policy or model behavior.
- **Unified audit storage foundation, opt-in** (roadmap M2/M3 plus the bounded M4 verifier; `src/storage/audit_migrate.py`, `src/audit/`). This lands the storage layer only: no graph node, terminal writer, trace sink or scoring path calls it yet, and the roadmap stays unfinished.
  - New tables: `audit_attempts`, `audit_addenda`, `audit_meta`, `audit_trace_io` and `audit_log_legacy`. An explicitly migrated `audit_log` holds one row per document with a unique `doc_id` and one global `seq`; before that it keeps the current per-event shape.
  - **The migration is never automatic.** Startup logs `audit_migration_pending` and changes nothing unless `MAILROOM_AUDIT_MIGRATE=1` (SQLite and Postgres alike). A fresh database still creates the new tables, but the marker, the unique indexes and the legacy copy wait for the explicit step. Ordinary runs keep writing the current `audit_log` through `write_audit_entry`.
  - `python -m storage.audit_migrate` runs it deliberately, under the `audit_log:chain` lock and in one transaction: SQLite is backed up to `<db dir>/backups/`, every `audit_log` row is copied byte-for-byte into `audit_log_legacy`, counts and a column digest are checked, and only then is `audit_log` emptied, given its unique indexes and recorded in `audit_meta` (`migrated_at`, `legacy_rows`, `legacy_tail_hashes_sha256`). A mismatch aborts with nothing changed; a second run is a no-op.
  - `--rollback` renames the new tables to `*_v2_<timestamp>` and restores the previous release's `audit_log` from `audit_log_legacy` (rows written since stay in the renamed tables).
  - The current writer and readers stay working through `event_log_model()`: after a deliberate migration they append to and read `audit_log_legacy`; before it they use the current `audit_log` with unchanged keys, order, sequences and hashes.
  - `write_terminal_row()`/`write_addendum()` (unused by the pipeline yet) write the unified row, attempt sub-rows and addenda with v2 hashes byte-identical to the dojo's `archive_entry_hash`; they refuse with `AuditMigrationPending` until the marker exists.
  - `audit.verify.verify_audit_db()` checks the global row chain, attempt membership/hashes, per-document addendum chains, the legacy per-document chain and stored trace-I/O hashes. It reports pending rows/sinks as uncollected rather than claiming proof, and `scripts/verify_audit_chains.py --json` exposes it.
  - Tests: `src/tests/test_audit_migrate.py` against `src/tests/fixtures/audit/legacy_audit.db` (rebuilt by `src/scripts/build_legacy_audit_fixture.py`), `test_audit_writer.py`, `test_audit_verify.py` and the default-off regression in `test_audit_log.py`. Postgres DDL is compiled for the `postgresql` dialect; the migration runs on SQLite.
