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

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:935f696589a79f921fbcf854c0af7c810b17622f93b1d24985d015cb66bcdf0f -->

- **Audit tables and legacy migration** (roadmap M2 of the unified audit row; `src/storage/audit_migrate.py`). The migration populates `audit_log_legacy` and `audit_meta`; the new row and addendum writers come in M3.
  - New tables: `audit_attempts`, `audit_addenda`, `audit_meta`, `audit_trace_io` and `audit_log_legacy`. `audit_log` itself now has a unique `doc_id` and one global `seq` (no longer counted per document).
  - `ensure_schema()` runs `migrate_audit_log()` once per database, under the `audit_log:chain` write lock and in one transaction. It copies every `audit_log` row into `audit_log_legacy` with a single `INSERT ... SELECT` (so each column keeps its stored value), checks the row count and a digest of every column, and only then empties `audit_log`, adds the unique `doc_id` and `seq` indexes and writes `audit_meta` (`migrated_at`, `legacy_rows`, `legacy_tail_hashes_sha256`). A mismatch aborts with nothing changed. It skips once `audit_meta` says it ran, and two processes starting together migrate once.
  - SQLite is backed up first, to `<db dir>/backups/` (or `backup_dir`). Postgres with rows to move runs only with `MAILROOM_AUDIT_MIGRATE=1`, after your `pg_dump`; without it the migration logs `audit_migration_pending` and changes nothing. An empty `audit_log` needs no opt-in.
  - `python -m storage.audit_migrate` runs the migration by hand; `--rollback` renames the new tables to `*_v2_<timestamp>` and restores the previous release's `audit_log` from `audit_log_legacy` (rows written since stay in the renamed tables).
  - `verify_legacy_chain()` (in the migration module for now) runs the per-document hash check over `audit_log_legacy`; M4 builds the full verifier around it.
  - Interim: until the row writers replace it, the per-event writer `write_audit_entry`, its readers and the warehouse and `verify_audit_chains.py` reads follow the migration through `event_log_model()`: they use `audit_log_legacy` once the migration has run and the old `audit_log` while it is pending. New events written in that window land in `audit_log_legacy` after `audit_meta.legacy_tail_hashes_sha256` was taken.
  - Tests: `src/tests/test_audit_migrate.py` against `src/tests/fixtures/audit/legacy_audit.db`, which `src/scripts/build_legacy_audit_fixture.py` rebuilds with the previous writer. Postgres DDL is compiled for the `postgresql` dialect; the migration runs on SQLite.


<!-- /coderabbit-ai-generated:v1 -->
- **Audit row and addendum writers** (roadmap M3; `src/audit/`). Nothing in the pipeline calls them yet; the terminal nodes adopt them in M7.
  - `write_terminal_row()` writes one `audit_log` row per document with the next global `seq`, under the `audit_log:chain` lock, with its `audit_attempts` sub-rows in the same transaction. A second terminal write for a document becomes a `resumed` addendum. A `seq` conflict is retried a bounded number of times, then raises `AuditWriteError`.
  - `write_addendum()` chains `audit_addenda` per document, starting from the row's hash (kinds `rescore`, `resumed`, `review_decision`, `relations_linked`, `recovered_detail`). It raises `NoAuditRow` when the document has no row.
  - Both refuse with `AuditMigrationPending` until the M2 migration has run. `get_row()` returns a row with its sub-rows and addenda.
  - Row hashes are byte-identical with the dojo's `archive_entry_hash`. Tests: `src/tests/test_audit_writer.py`.
