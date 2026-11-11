# Audit review disposition ledger

Original audit head: `3bb9eec21d5704f4a1e223aed3d02691c5437607`. These are source-level validity verdicts, **not claims that fixes have passed**. Final fix commits/tests must be added after verification.

GitHub snapshot: 29 PR160 threads, 23 unresolved; 32 PR215 threads, 0 unresolved. Both thread connections report `hasNextPage=false`, and no comment connection has another page. The PR evidence index retains the provider snapshot. Provider resolution does not by itself prove a claim was fixed.

| Finding | Verdict | Source-level assessment |
|---|---|---|
| [4458955010](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4458955010) | STALE | Changelog now distinguishes migration writes from unused terminal writers. |
| [4458955027](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4458955027) | STALE | Pending hint already includes MAILROOM_AUDIT_MIGRATE=1. |
| [4458955071](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4458955071) | STALE | Current event writer takes the shared PostgreSQL migration gate before selecting its table; older-writer coordination is a separate valid finding. |
| [4494810886](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494810886) | STALE | Warehouse already chunks large document-id queries. |
| [4494829171](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494829171) | DO NOT ACCEPT | scripts exclusion also exists at the audit merge base; this is a pre-existing wheel/eval packaging issue, outside this audit change. |
| [4494829188](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494829188) | STALE | Unknown started_at is already normalized as None and has a regression. |
| [4494829205](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494829205) | ACCEPT | Fixture builder leaves DATABASE_URL/schema cache pointing at its deleted temporary database. |
| [4494839682](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494839682) | STALE | Hash documentation already names archive_entry_hash and detail normalization. |
| [4494839699](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494839699) | STALE | Duplicate unknown-start-time finding; None is handled. |
| [4494839713](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494839713) | STALE | Shared migration gate already exists in the event writer; document-key collision remains a separate finding. |
| [4494839730](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4494839730) | STALE | Atomicity test now injects after attempt flush using Session.add and checks rollback. |
| [4504099224](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099224) | ACCEPT | Writer docstrings contradict the supported None start time. |
| [4504099227](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099227) | ACCEPT | Shallow detail copies can change across an awaited flush after hashing. |
| [4504099229](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099229) | ACCEPT | A repeated terminal write reuses attempt primary keys in its resumed addendum. |
| [4504099233](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099233) | ACCEPT | get_row can read legacy events as a unified row before migration. |
| [4504099238](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099238) | ACCEPT | get_row queries need one consistent read snapshot. |
| [4504099241](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099241) | ACCEPT | Fixture backup opens a source sqlite connection without explicitly closing it. |
| [4504099244](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099244) | ACCEPT | The verification CLI checks legacy events alone and can miss corrupt unified rows. |
| [4504099247](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099247) | ACCEPT | Event readers can select the pre-migration table and then observe it emptied unless migration and reads coordinate. |
| [4504099250](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099250) | ACCEPT | Buffered whole-table fingerprints/tail dictionaries grow with all audit history. |
| [4504099252](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099252) | ACCEPT | sqlite.connect creates backup files under the ambient umask, potentially 0644. |
| [4504099254](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099254) | ACCEPT | Older PostgreSQL writers do not honor the new advisory gate; source-table locking and a quiesced upgrade are needed. |
| [4504099260](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099260) | ACCEPT | Notebook audit_chain still selects audit_log instead of migrated event history. |
| [4504099270](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504099270) | LIKELY ACCEPT | Schema create_all check-first races across processes; the new migration requires an independently verified startup path. |
| [4504101973](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504101973) | ACCEPT | Addendum writes need the migration/rollback gate before taking document locks. |
| [4504101975](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504101975) | ACCEPT | No-attempt addenda can inherit references to another parent from detail.attempts. |
| [4504101979](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504101979) | ACCEPT | doc_id=chain collides with the global audit_log:chain advisory key. |
| [4504101982](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504101982) | ACCEPT | The startup test uses threads sharing a cache/lock; it does not prove independent-process behavior. |
| [4504101985](https://github.com/Exios66/llm-mailroom/pull/160#discussion_r4504101985) | ACCEPT | Attempt-hash tests need independent mutations of envelope fields, rather than only comparing the helper with itself. |

PR215 core journal transition and array-item findings were still valid at historical `2eaeb7a`, and are fixed in Claude commits `e7209ae` / `6cb8fda`. The latest head was preserved. Remaining object-valued text leaves were reproduced as accepted by the backend; the separate audit workbench layer validates and projects them. No PR215 source changes were pushed by this audit pass.


## Reconciliation and verification contract

The replacement foundation preserves the original audit hash algorithms and current
per-event writers. Migration is opt-in on both database engines. The diagnostics viewer
is a separate dependent PR and never initializes or migrates a store.

The ACCEPT and LIKELY ACCEPT items above have implementations in this foundation. Final
acceptance requires the exact committed tree and command receipts in its PR; the source
verdicts above are not substituted for those receipts. No remote review thread is marked
resolved by this document.

| Review concern | Implementation | Reproduction / verification |
| --- | --- | --- |
| Default-off startup and event compatibility | `storage/db.py`, `storage/audit_log.py` | `test_default_schema_is_legacy_compatible_until_explicit_migration`, `test_legacy_orm_seq_default_is_zero` |
| Original data, backup and rollback | `storage/audit_migrate.py` | `test_migrates_legacy_rows_byte_for_byte`, `test_counts_or_hash_mismatch_aborts_without_emptying`, `test_sqlite_backup_written`, `test_rollback_restores_audit_log` |
| Old SQLite sequence columns | nullable legacy sequence plus total keyset ordering | `test_tail_digest_handles_null_seq_added_column` also runs rollback without replacing null sequences |
| Separate worker startup | schema DDL retry plus transactional migration | `test_independent_processes_migrate_once` |
| PostgreSQL migration coordination | source table lock before authoritative count; shared gates before document locks | `test_postgres_table_lock_precedes_deciding_count`, `test_event_writers_wait_for_migration_before_selecting_model`; these are local seams, not live PostgreSQL proof |
| Immutable input and repeated attempts | deep snapshots and addendum-scoped attempt identities | `test_detail_is_snapshotted_before_hashing`, `test_repeating_the_exact_terminal_row_reidentifies_attempts`, `test_addendum_without_attempts_clears_inherited_attempts` |
| Read consistency and migration state | one read snapshot, migration marker guard | `test_get_row_reads_one_snapshot`, `test_get_row_refuses_while_migration_pending` |
| Complete local storage verification | `audit/verify.py`, `scripts/verify_audit_chains.py` | `test_audit_verify.py`: main/attempt/addendum/legacy/I/O corruption, parent membership, malformed JSON and null detail |
| Notebook legacy event history | marker-selected event table in `notebooks/pipeline_lab.py` | Compile checked; runtime notebook rendering remains explicitly unverified |

Independent local review also found and corrected cross-parent attempt references,
pre-sequence rollback failure, malformed stored JSON crashes, and null-detail false
verification. The PR review log names each review round and the final checks.

The offline verifier materializes audit tables in memory. Its scope is local storage;
pending manifests, external sinks and the M5–M15 writer/scoring rollout remain unfinished.
Do not interpret a clean storage report as proof that those uncollected stages ran.
