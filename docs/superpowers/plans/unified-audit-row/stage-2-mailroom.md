# Stage 2: mailroom, one audit row per document

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every document the graph finishes writes exactly one hash-chained `audit_log` row. The row carries its attempt sub-rows, a trace of every node visit, the dojo scoring block and the metrics. Later events are addenda, and the old rows are kept as a verifiable legacy section.

**Architecture:** Nodes append visits to `state["audit_trace"]` through `traced_node`. The scoring node family follows the dojo registry. Terminal nodes (`archive`, `audit_failed`, the triage lane, the watcher abort path) build the row from state and write it with `write_terminal_row` under one global lock. Trace I/O goes to pluggable sinks (`sql` always on); a row that cannot be committed is parked in the sinks and reconciled later.

**Tech Stack:** Python 3.11, LangGraph, SQLAlchemy async (SQLite default, Postgres via `DATABASE_URL`), FastAPI, pyarrow, pytest. Work on branch `claude/unified-audit-row` in `llm-mailroom`.

**Spec:** `docs/superpowers/specs/2026-11-03-unified-audit-row-design.md` §4–§8

## Global Constraints

- Hash version 2 is unchanged and byte-identical with the dojo's `format_audit_entry`. `seq` is not hashed.
- Fields not collected on a run are `null`, never filled in.
- `audit.full_trace` / `MAILROOM_AUDIT_FULL_TRACE` defaults to off.
- The current graph order stays as it is: judge before arbiter (KANBAN-063), and the boss escalation path.
- `emit_pipeline_scores` output is unchanged; `detail.metrics` is exactly that output plus `subclass_correct`.
- Gate per task: `pytest -q` (testpaths `src/tests`) and `python src/scripts/roadmap_status.py --check` pass.
- Public API additions only: `/audit/{doc_id}` and `/audit` keep every existing key.

## Review Focus

- Postgres (`DATABASE_URL=postgresql+asyncpg://…`): migration, unique index and the global lock must work. Tests compile all DDL for the `postgresql` dialect and run the migration on SQLite.
- The document type changes during the run (classification arbiter, or a person's override on the tray): scoring routes on the final type, while sub-rows keep the type each attempt had.
- The extraction is not a dict (prose, `None`, a parse error): the row is still written, with `extraction.adherence.parse_error = true` and a scoring block whose `status` explains the result.
- `full_trace` is on and the document is long: the row is written, and when a sink rejects the payload size the fallback sends the decisions-only record plus the full record's SHA-256.
- A document that already has legacy rows is processed again after migration: a new row is written and the legacy rows are untouched. A second terminal write for the same `doc_id` becomes a `resumed` addendum.

---

### Task M1: Adopt dojo 0.27.0

**Repo:** llm-mailroom
**Spec:** §9
**Depends:** D8

**Files:**
- Modify (through the bump PR): `pyproject.toml` dojo pin (today `e35504db`, 0.25.0), `uv.lock`, `vendor/llm-dojo-scoring` (the fallback snapshot, also 0.25.0)

- [x] **Step 1: Wait for the PR that `bump-dojo-scoring.yml` opens** for `v0.27.0` (review item A9). Close any unmerged 0.26.0 pin branch as superseded (check the workflow's real branch name, `cursor/bump-dojo-scoring-<tag>`, before closing anything).
- [x] **Step 2: On that PR**, run `pytest -q`. Expected: PASS. The jump is 0.25.0 → 0.27.0 (gap G1): 0.27.0 is additive, but 0.26.0 is not, so diff field-scoring output on the mailroom golden/eval fixtures before and after the bump, and record every score that moves, with its 0.26.0 CHANGELOG entry, in the PR body.
- [x] **Step 2b: Re-vendor the fallback snapshot** to 0.27.0 and add `test_scoring_imports_under_vendored_source`: the dojo names the scoring nodes need (`score_document`, `get_doc_scorer`, `score_archive_block`, `assess_extraction_payload`) import with `MAILROOM_DOJO_SOURCE=vendored` (gap G2).
  - Done in #157. The workflow is disabled in this repo, so `bump_dojo_scoring.py` made the branch by hand; it supersedes #149 (0.26.0). G1: one insurance CMS row moves (`cms_source_table`, #93); the mock experiment is unchanged. Step 3 is committed there and waits for the owner merge.
- [ ] **Step 3: Extend `src/tests/test_dojo_parity.py`** (it already checks field maps at line 38) so it covers all five classes against dojo `DEFAULT_FIELD_TYPES`, and add `test_no_extract_class_aliases` (`EXTRACT_CLASS_ALIASES` is already `{}` in `pipeline/config.py`; the test pins it). Commit, then merge (owner).

### Task M2: Audit tables and migration

**Repo:** llm-mailroom
**Spec:** §4, §4.1, §4.2, §4.3, §4.5, §6
**Depends:** M1

**Files:**
- Modify: `src/storage/audit_log.py`. `AuditLogRecord` gets a unique `doc_id` and a global `seq`. Add new models `AuditAttemptRecord`, `AuditAddendumRecord`, `AuditLegacyRecord`, `AuditMetaRecord` and `AuditTraceIORecord`.
- Create: `src/storage/audit_migrate.py`
- Modify: `src/storage/db.py`. `ensure_schema` calls `migrate_audit_log`.
- Test: `src/tests/test_audit_migrate.py`; fixture `src/tests/fixtures/audit/legacy_audit.db`, built by a fixture script from today's writer.

**Interfaces:**
- Produces: `async def migrate_audit_log(*, backup_dir: Path | None = None) -> MigrationResult` (`migrated: bool`, `legacy_rows: int`, `backup_path: str | None`) and `async def rollback_audit_migration() -> RollbackResult`. CLI: `python -m storage.audit_migrate [--rollback]`.

- [ ] **Step 1: Write the tests.**
  - `test_migrates_legacy_rows_byte_for_byte`: every legacy column equals the original, and `verify_legacy_chain` passes.
  - `test_migration_is_idempotent`: a second run gives `migrated=False` and no changes.
  - `test_concurrent_startup_migrates_once`: two `ensure_schema` calls run under `asyncio.gather`.
  - `test_counts_or_hash_mismatch_aborts_without_emptying`.
  - `test_sqlite_backup_written`.
  - `test_postgres_requires_opt_in`: with a Postgres URL and no `MAILROOM_AUDIT_MIGRATE=1`, the result is `migrated=False` and `audit_migration_pending` is logged.
  - `test_ddl_compiles_for_postgres`.
  - `test_rollback_restores_audit_log`: new tables are renamed `*_v2_<ts>`, and `audit_log` holds the legacy rows again.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Everything runs under `acquire_write_lock(session, "audit_log:chain")`. Check `audit_meta` first. Add unique indexes on `audit_log.doc_id` and `audit_log.seq`. SQLite: copy the DB file to `backup_dir` (default `<db dir>/backups/`). Postgres: proceed only with the opt-in, in one transaction. Write `audit_meta = {migrated_at, legacy_rows, legacy_tail_hashes_sha256}`.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M3: Row and addendum writers

**Repo:** llm-mailroom
**Spec:** §4, §4.1, §4.2, §4.3, §5.3
**Depends:** M2

**Files:**
- Create: `src/audit/__init__.py`, `src/audit/writer.py`, `src/audit/models.py`
- Test: `src/tests/test_audit_writer.py`

**Interfaces:**
- Produces:
  - Dataclasses `TerminalRow(doc_id, matter_id, event, actor, detail, attempts: list[AttemptRow])`, `AttemptRow(attempt_id, stage, attempt_no, trigger, outcome, started_at, ended_at, detail)` and `Addendum(doc_id, kind, actor, detail, attempts=[])`.
  - `async def write_terminal_row(row: TerminalRow) -> WriteResult` (`entry_id`, `entry_hash`, `kind: "row" | "addendum"`).
  - `async def write_addendum(add: Addendum) -> WriteResult`.
  - `async def get_row(doc_id) -> dict | None`.
  - `EVENTS = ("archived", "failed", "triage_archived", "triage_reviewed")` and `ADDENDUM_KINDS = ("rescore", "resumed", "review_decision", "relations_linked")`.

- [ ] **Step 1: Write the tests.**
  - `test_row_hash_matches_dojo_format_audit_entry`: compare against `llm_dojo_scoring.format_audit_entry` on the same inputs.
  - `test_rows_chain_globally_by_seq`.
  - `test_subrow_hashes_listed_in_parent_detail_attempts`.
  - `test_subrows_and_row_commit_atomically`: a failure injected after the sub-row insert leaves neither.
  - `test_second_terminal_write_becomes_resumed_addendum`: its sub-rows have `parent_id` = the addendum id.
  - `test_seq_conflict_retries`: two processes race on SQLite, where `acquire_write_lock` does not cross processes. The unique `seq` index rejects one insert, and the writer re-reads the tail and retries (review item A17).
  - `test_addendum_chain_starts_at_row_hash`.
  - `test_addendum_without_row_raises_NoAuditRow`.
  - `test_concurrent_documents_do_not_fork_chain`: 20 documents under `asyncio.gather`.
  - `test_unknown_event_rejected`.
  - `test_writer_refuses_while_migration_pending`: with no `audit_meta` (Postgres opt-in not given), `write_terminal_row` raises `AuditMigrationPending` and writes nothing to `audit_log` (spec §6). M10 routes that error into the §5.6 fallback.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Hash with the existing `compute_audit_hash` (v2). Sub-row `entry_id` = `attempt_id`, `event` = `attempt:<stage>`. The parent's `detail.attempts` is filled with sub-row hashes before the parent is hashed. Use the global lock `audit_log:chain` for rows and `audit_log:<doc_id>` for addenda.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M4: Verification

**Repo:** llm-mailroom
**Spec:** §6
**Depends:** M3

**Files:**
- Modify: `src/schemas/audit.py`. Rename today's per-document `verify_chain` to `verify_legacy_chain`. `verify_chain` keeps its name as an alias that dispatches by input shape until the readers in M11 move over.
- Create: `src/audit/verify.py`
- Modify: `src/scripts/verify_audit_chains.py`
- Test: `src/tests/test_audit_verify.py`

**Interfaces:**
- Produces: `async def verify_audit_db(*, check_sinks: bool = False) -> VerifyReport`. `VerifyReport` has `main`, `subrows`, `addenda`, `legacy`, `trace_io` and `pending`, each `{ok: bool, checked: int, failures: list[dict]}`, plus `ok`.

- [ ] **Step 1: Write the tests.**
  - `test_clean_db_verifies`.
  - `test_tampered_row_detail_fails_main`.
  - `test_deleted_subrow_fails_subrows`.
  - `test_extra_subrow_fails_subrows`.
  - `test_edited_addendum_fails_addenda`.
  - `test_tampered_legacy_fails_legacy_only`.
  - `test_io_sha_mismatch_fails_trace_io`.
  - `test_recovered_row_detail_sha_checked`.
  - `test_pending_manifest_without_row_reported`.
  - `test_script_exits_nonzero_on_failure`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the five checks and the pending report.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M5: Trace collection in state

**Repo:** llm-mailroom
**Spec:** §4.4, §5.2
**Depends:** M1

**Files:**
- Create: `src/audit/trace.py`
- Modify: `src/graph/state.py`. Add `audit_trace: Annotated[list[dict], append_visits]`.
- Modify: `src/observability/tracing.py` (`traced_node`)
- Modify: `src/graph/build_graph.py`. The 7 `_emit_stage_audit` call sites become `record_visit` decisions, and `_emit_stage_audit` is removed.
- Test: `src/tests/test_audit_trace.py`, plus a sandbox contract fixture `src/tests/fixtures/audit/sandbox_decision_schema.json`

**Interfaces:**
- Produces:
  - `NODE_DECISION_FIELDS: dict[str, tuple[str, ...]]`, the per-node state fields. `intake` includes `bert_triage`.
  - `append_visits(left: list, right: list) -> list`.
  - `def visit(node: str, state: dict, update: dict, *, started_at, ended_at, status, llm: LLMCallInfo | None) -> dict`, which returns a §4.4 visit record.
  - `attempt_id_for(state: dict, node: str) -> str`, which is deterministic from `doc_id`, stage and counters.
  - `decisions_for(node, state, update) -> list[dict]` in the sandbox schema `{question, type, answer, p, expected, options, source}`.

- [ ] **Step 1: Write the tests.**
  - `test_traced_node_appends_one_visit_per_call`.
  - `test_visit_fields_match_spec`: exactly the §4.4 keys, with `prompt` and `completion` only when `full_trace` is on.
  - `test_decisions_match_sandbox_schema`: validate against the fixture copied from the sandbox's `job/node_trace.py`.
  - `test_attempt_ids_follow_retry_counters`.
  - `test_intake_visit_carries_bert_triage`.
  - `test_trace_survives_checkpoint_resume`.
  - `test_old_checkpoint_without_trace_resumes` (review item A3): resume a recorded pre-upgrade checkpoint fixture; the trace starts empty and `trace_incomplete` is set.
  - `test_no_stage_rows_written_during_run`: `audit_log` stays empty until a terminal node runs.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `traced_node` measures timing and reads the LLM call info that the LLM wrapper already records (tokens, cost, model, prompt version). It hands prompt and completion to the sink layer (M9) through `audit.sinks.record_io`; until M9 lands, that is a no-op returning `(None, sha256)`.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M6: Scoring node family

**Repo:** llm-mailroom
**Spec:** §3.1, §5.1
**Depends:** M5

**Files:**
- Create: `src/graph/scoring_nodes.py`
- Modify: `src/graph/build_graph.py`. The edge changes from `compile_report → catalog_write` to `compile_report → score_common → score_<type> → catalog_write`.
- Modify: `src/observability/extraction_gt.py` (gap G3). Today it flattens CUAD/MAUD labels into `cuad_clauses` / `maud_clauses` and drops the raw `cuad_clause_labels`. The dojo type modules read the raw `cuad_clause_labels` / `maud_clause_labels` maps, so the ground-truth adapter for scoring must pass them through unchanged.
- Test: `src/tests/test_scoring_nodes.py`

**Interfaces:**
- Consumes: dojo `DOC_SCORERS`, `get_doc_scorer`, `score_archive_block(..., doc_subclass=)`, `assess_extraction_payload(..., schema_lookup=)`.
- Produces:
  - `add_scoring_nodes(graph: StateGraph, *, next_node: str = "catalog_write") -> None`.
  - `score_route(state) -> str`, which returns `score_<type>` or `score_generic`.
  - State keys `scoring_common` (adherence plus the tier-0 result), `scoring` (the archive block plus `scored_payload`) and `extraction_adherence`.
  - `score_document_block(state_like: dict) -> dict`, the pure function the nodes and the rescore CLI (M14) share.

- [ ] **Step 1: Write the tests.**
  - `test_one_node_per_registry_entry`.
  - `test_routes_each_corpus_type`, parametrised over the five types.
  - `test_corpus_absent_and_unknown_route_to_generic`.
  - `test_routes_on_final_doc_type_after_override` (Review Focus).
  - `test_no_ground_truth_is_unscorable`.
  - `test_non_dict_extraction_still_scored` (Review Focus).
  - `test_field_map_sha_from_mailroom_taxonomy`.
  - `test_each_scoring_node_has_trace_visit`.
  - `test_benchmark_rows_get_headline` (G3): CUAD and MAUD rows reach `score_document_block` with their raw label maps, and the block has `headline_score` and `headline_metric`.
  - `test_doc_subclass_from_final_state` (G5): the scoring call gets the final `state["doc_subclass"]` (set near `build_graph.py:831`), not the classifier's first guess and not nothing.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `schema_lookup` is `schemas.documents.get_extraction_schema`. `taxonomy_bytes` is read once from `src/config/taxonomy.yaml`.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M7: Row builder and terminal nodes

**Repo:** llm-mailroom
**Spec:** §4.1, §4.2, §5.1, §5.3
**Depends:** M3, M6

**Files:**
- Create: `src/audit/row.py`
- Modify: `src/agents/archivist.py`. `archive_document` writes the row through `write_terminal_row`, and its `detail` comes from `build_row`.
- Modify: `src/graph/build_graph.py`. Add the `audit_failed` node and make every stop edge (`human_review → failed`, judge stop, intake failure, rejected review) target it. Wire the watcher abort path and the triage lane writers.
- Create: `audit-row-schema.md` under `docs/` (new in M13). It is the row reference and says which `trace` is which (review item A12). It also defines `schema_valid` once (gap G6): today `observability/scores.py` `validate_extraction` returns a bool while dojo adherence reports a fraction. The row uses the dojo's `schema_adherence` and `schema_valid`, and `validate_extraction` delegates to `assess_extraction_payload`.
- Test: `src/tests/test_audit_row.py`, `src/tests/test_audit_terminal_paths.py`

**Interfaces:**
- Produces: `build_row(state: dict, *, event: str, actor: str) -> TerminalRow` and `attempts_from_trace(trace: list[dict]) -> list[AttemptRow]`. The `detail` keys are exactly those in the spec §4.1 table, with `row_schema: 1`.

- [ ] **Step 1: Write the tests.**
  - `test_detail_keys_exactly_spec`.
  - `test_uncollected_fields_are_null`.
  - `test_metrics_equal_emit_pipeline_scores`.
  - `test_success_fixture_writes_one_row_intake_to_archive`.
  - `test_judge_stop_writes_failed_row_naming_node`.
  - `test_tray_park_then_fail_writes_failed_row`.
  - `test_triage_lane_writes_one_row`.
  - `test_watcher_abort_writes_failed_row`.
  - `test_subrows_per_retry_and_arbiter_rescue`.
  - `test_trace_incomplete_flag_on_old_checkpoint`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M8: Human review paths and pre-row events

**Repo:** llm-mailroom
**Spec:** §4.3, §5.4
**Depends:** M7

**Files:**
- Modify: `src/pipeline/review_resolve.py`. `complete_human_extraction` and the `complete` disposition resume the graph at `compile_report` instead of archiving directly.
- Modify: `src/api/main.py` (review dispositions), `src/graph/build_graph.py` (`resume_from_review` entry point at `compile_report`)
- Modify: the relations step, which writes an addendum only when a row exists and otherwise records a `relations` visit.
- Test: `src/tests/test_review_paths_audit.py`

**Interfaces:**
- Produces: `async def record_document_event(doc_id: str, kind: str, detail: dict) -> Literal["addendum", "trace"]`. It writes an addendum when the row exists; otherwise it appends a visit to the parked checkpoint through `graph.update_state(config, {"audit_trace": [visit]})`, so the reducer applies (review item A2).

- [ ] **Step 1: Write the tests.**
  - `test_complete_without_data_scores_and_archives_model_extraction`: `pipeline_success` is false and `extraction_source` is `"model"`.
  - `test_complete_with_data_is_human_correction`: a `human_correction` sub-row carrying the diff, reviewer and time; `scored_payload` is `"human_corrected"`; no extract node in the trace after the review.
  - `test_resume_reruns_extract`.
  - `test_doc_type_override_reruns_extraction_and_routes_scoring`.
  - `test_record_and_requeue_before_row_land_in_trace`.
  - `test_record_after_row_is_review_decision_addendum`.
  - `test_model_scores_not_credited_with_correction`: the last model attempt's scores are kept on its sub-row.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M9: Trace sinks

**Repo:** llm-mailroom
**Spec:** §5.5
**Depends:** M5

**Files:**
- Create: `src/audit/sinks/__init__.py` (protocol, registry, `record_io`), `sql.py`, `phoenix.py`, `langfuse.py`, `braintrust.py` and `otlp.py`
- Modify: `src/config/taxonomy.yaml`, adding `audit: {full_trace: false}`, and `pyproject.toml`, adding the `mailroom.trace_sinks` entry-point group
- Test: `src/tests/test_trace_sinks.py`

**Interfaces:**
- Produces:
  - `class TraceSink(Protocol)` with `name`, `record_io(visit_id, doc_id, prompt, completion) -> str | None` (returns a ref), `capture_pending(record: dict) -> str | None`, `fetch_pending() -> list[dict]` and `fetch_io(ref) -> dict | None`.
  - `active_sinks() -> list[TraceSink]`, ordered with `sql` first.
  - `record_io(...) -> tuple[str | None, str, list[str]]`, returning `(trace_sink, io_sha256, also_in)`.

- [ ] **Step 1: Write the tests.**
  - `test_sql_sink_always_active`.
  - `test_phoenix_added_when_importable_enabled_and_reachable`: use a fake collector on localhost and the 1-second probe.
  - `test_phoenix_skipped_on_railway_localhost_endpoint`.
  - `test_env_enables_opt_in_sinks`: `MAILROOM_TRACE_SINKS=sql,langfuse`.
  - `test_entry_point_sink_loaded`.
  - `test_sink_failure_does_not_fail_node`.
  - `test_full_trace_copies_io_into_visit`.
  - `test_io_sha_is_sha256_of_prompt_and_completion`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The adapters reuse the clients in `src/observability/tracing.py`.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M10: Write-failure fallback and `audit-reconcile`

**Repo:** llm-mailroom
**Spec:** §5.6
**Depends:** M7, M9

**Files:**
- Modify: `src/audit/writer.py`, adding the retry and pending capture
- Modify: `src/schemas/manifest.py`. `audit_written` can now be `true`, `false` or `"pending"`, and the manifest gains `audit_pending_refs`.
- Create: `src/scripts/audit_reconcile.py`
- Test: `src/tests/test_audit_fallback.py`

**Interfaces:**
- Produces: `async def reconcile(*, dry_run: bool = False) -> ReconcileReport` (`recovered`, `skipped_existing`, `rejected_hash`). The spool path comes from `MAILROOM_AUDIT_SPOOL_PATH`, defaulting to `<db dir>/audit_spool.db`.

- [ ] **Step 1: Write the tests.**
  - `test_three_failures_then_pending_capture`.
  - `test_manifest_marked_pending_and_run_succeeds`.
  - `test_oversize_payload_sends_decisions_only_plus_sha` (Review Focus).
  - `test_reconcile_writes_recovered_row_that_verifies`.
  - `test_reconcile_dedupes_across_sinks`.
  - `test_reconcile_rejects_hash_mismatch`.
  - `test_reconcile_skips_doc_with_row`.
  - `test_no_sink_accepts_logs_audit_gap`.
  - `test_ephemeral_spool_warning` (review item A6).
  - `test_migration_pending_takes_fallback`: `AuditMigrationPending` from the writer is handled like a failed commit; the record is parked and `audit-reconcile` writes it after the migration (spec §6).
  - `test_decisions_only_copy_reconciles_as_partial`: with only a decisions-only copy, reconcile writes it with `recovered_from.complete = false`, and logs `audit_gap_partial`; the manifest said `pending_partial`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M11: Readers and `audit_row_metrics`

**Repo:** llm-mailroom
**Spec:** §7
**Depends:** M4, M8

**Files:**
- Create: `src/audit/metrics.py`
- Modify: `src/api/main.py` (`GET /audit/{doc_id}`, `GET /audit`), `src/storage/audit_log.py` (`analyze_audit_db`), `src/scripts/analyze_audit_db.py`
- Test: `src/tests/test_audit_api.py`, `src/tests/test_audit_metrics.py`

**Interfaces:**
- Produces: `audit_row_metrics(row: dict, addenda: list[dict], attempts: list[dict]) -> dict` with exactly the spec §7 keys. `overall_extraction_score` is `scoring.headline_score` when set, else `scoring.overall_score`; `suite_overall_extraction_score` is `scoring.overall_score` (review item A1).

- [ ] **Step 1: Write the tests.**
  - `test_audit_doc_contract`: the keys `doc_id`, `chain_length`, `chain_valid` and `entries` keep their names, and the new keys are `legacy`, `pending` and `warehouse`. Entries are ordered row, sub-rows, then each addendum with its sub-rows, and each has `kind` and `parent_id`.
  - `test_audit_summary_rates`.
  - `test_metrics_keys_exactly_spec`.
  - `test_latest_rescore_supplies_scores`.
  - `test_resumed_supplies_outcome`.
  - `test_model_only_counterparts_from_last_model_attempt`.
  - `test_headline_mapping`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M12: Warehouse, `audit-report` and Langfuse scores

**Repo:** llm-mailroom
**Spec:** §7
**Depends:** M11

**Files:**
- Modify: `src/storage/warehouse.py`, `src/scripts/export_warehouse.py`
- Create: `src/scripts/audit_report.py`
- Modify: `src/observability/scores.py`. When Langfuse is configured, also emit `overall_score`, `extraction_f1`, `extraction_f2` and `schema_adherence`.
- Test: `src/tests/test_warehouse_audit.py`, `src/tests/test_audit_report.py` with the golden `src/tests/fixtures/audit/audit_report.md`

- [ ] **Step 1: Write the tests.**
  - `test_parquet_round_trip_rows_attempts_addenda`: keyed merge by `entry_id`, `attempt_id` and `addendum_id`.
  - `test_legacy_exported_once`.
  - `test_manifest_maps_doc_to_hash_and_files`.
  - `test_audit_report_golden`: per live type it shows the schema, n, model, route, F1 and F2, with uncollected metrics labelled.
  - `test_report_reads_db_or_parquet_identically`.
  - `test_langfuse_scoring_emission_names`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M14: Rescore CLI

**Repo:** llm-mailroom
**Spec:** §4.3, §5.3
**Depends:** M3, M6

**Files:**
- Create: `src/scripts/audit_rescore.py`
- Test: `src/tests/test_audit_rescore.py`

**Interfaces:**
- Consumes: `write_addendum`, `get_row` (M3); the scoring node family's `score_document_block(state_like: dict) -> dict` helper (M6).
- Produces: `async def rescore(doc_ids: list[str] | None = None, *, dry_run: bool = False) -> RescoreReport` (`rescored`, `unchanged`, `skipped_no_gt`). CLI: `python src/scripts/audit_rescore.py [--doc-id ...] [--dry-run]`.

- [ ] **Step 1: Write the tests.**
  - `test_rescore_writes_addendum_not_row`.
  - `test_unchanged_score_writes_nothing`.
  - `test_rescore_uses_final_extraction_and_doc_type_from_row`.
  - `test_rescore_records_old_and_new_dojo_version`: the addendum also records the ground-truth dataset revision.
  - `test_rescore_without_gt_entry_is_skipped`: no `ground_truth` row for the document's filename → `skipped_no_gt`, no addendum.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Read `extraction.extracted_data`, `doc_type` and `doc_subclass` from the row. The row's `refs` locate the document, not its ground truth. Ground truth comes from the same source the graph scores against: the pinned `mailroom-dataset` revision's `ground_truth` config (the revision `sync_hf_ground_truth.py` pins), looked up by the document's source filename from the manifest at `refs.manifest_path` and converted with `observability.extraction_gt.build_expected_fields`. A document with no entry is counted in `skipped_no_gt` and gets no addendum. Score through the same helper the graph uses, and record the dataset revision in the addendum next to the old and new dojo versions.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M15: One scoring path (retire the legacy scorers)

**Repo:** llm-mailroom
**Spec:** §3, §7
**Depends:** M6

Gap G4: the mailroom never calls `score_archive_block` today. It scores in three older places: `src/graph/build_graph.py` (around line 3799), `src/observability/suite_scoring.py` and `src/scripts/run_hf_pilot.py` (around line 598). If they stay next to the scoring node family, judge gating and Langfuse report a different overall number from the audit row.

**Files:**
- Modify: those three call sites, so they read the row's `scoring` block or call `score_document_block`.
- Test: `src/tests/test_one_scoring_path.py`

- [ ] **Step 1: Write `test_judge_and_langfuse_use_row_score`.** For a fixture document, the judge's gating score and the Langfuse `overall_score` equal the row's `scoring.overall_score` (the field-map mean), and `scoring.headline_score` is checked on its own: the MAUD/CUAD headline for a contract or merger fixture with clause labels, else null. The test does not require `headline_score` to equal `overall_score`. Add `test_no_direct_suite_scoring`, a grep-style test that `suite.score(` / `score_extraction(` are not called outside `graph/scoring_nodes.py` and the dojo.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Route the three paths through `score_document_block`,** or the row's block when it already exists.
- [ ] **Step 4: Run them.** Expected: PASS. Run the full suite.
- [ ] **Step 5: Commit.**

### Task M13: Docs and branch gate

**Repo:** llm-mailroom
**Spec:** §8, §9
**Depends:** M10, M12, M14, M15

**Files:**
- Modify: `docs/architecture.md`. Describe the current graph (judge before arbiter, boss), the scoring node family and `audit_failed`.
- Modify: `CHANGELOG.md`, and the deploy notes (migration, Postgres opt-in, rollback, spool path; on Postgres the migration runs before, or with, the release that ships the writers, spec §6)

- [ ] **Step 1: Run** `pytest -q`. Expected: PASS. Then `python src/scripts/verify_audit_chains.py` on a DB produced by `pytest`'s end-to-end fixture. Expected: exit 0.
- [ ] **Step 2: Update the docs.** Run `python src/scripts/roadmap_status.py --check`. In llm-dojo-scoring, regenerate `tests/fixtures/consumer_surface.json` (`scripts/audit_consumer_surface.py --write`) now that the mailroom imports `score_document`; `contract_surface.json` keeps pinning the names until then.
- [ ] **Step 3: Commit, then open the PR** (owner asks). Tick M1–M14 in the same PR.
