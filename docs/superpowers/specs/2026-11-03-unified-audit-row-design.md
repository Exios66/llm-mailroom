# Unified audit row: one hash-chained row per document

- **Date:** 2026-11-03
- **Status:** design approved in conversation; spec awaiting review
- **Issues:** LLM-Mailroom-Services/mailroom-issues #236, #237, #238, #239 (this spec),
  #240 and #241 (hub follow-ups that depend on it), #235 (field maps, already met)
- **Repos touched:** `llm-dojo-scoring` (scoring), `llm-mailroom` (pipeline, audit, API),
  `local-mailroom-sandbox` (vendor refresh, thin wrapper), `Digital-Mailroom` (sync only)

## 1. Goal

Every document the pipeline finishes, whether it succeeded or failed, has exactly one
hash-chained row in `audit_log`. That row holds the full record of the run: the path
through the graph, what every agent decided, every attempt, the extraction, the scores,
the outcome, and references to the report, archive file and warehouse export. Retries
and second attempts are sub-rows tied to that parent row. Events after the row is written
(rescores, resumed runs, review decisions, relation links) are addenda, also hash-linked.
Old per-stage rows move to a frozen legacy section that stays verifiable.

One scorer produces the numbers everywhere. The dojo owns it. Mailroom files its output
on the row; the sandbox keeps writing its own outputs with it.

### Non-goals

- Changing the sandbox's own outputs (`items.jsonl`, scores JSONL,
  `experiment_log.jsonl`, serving JSON, Modal telemetry, cards, hub). They stay as they are.
- Updating The-Mailroom. It is outdated; it catches up to the contract here separately.
- Reordering the judge and arbiter. The current order (judge, then arbiter, then
  `retry_extract`; KANBAN-063) is kept. #236's flowchart is superseded on this point.
- Retiring eval-environment's copy of extraction scoping (optional follow-up).

## 2. Issue staleness (checked 2026-11-03)

| Claim | Status | Decision |
|---|---|---|
| Dojo pin of record `8a38f38` / v0.19.1 | Stale: dojo is 0.26.0 | Target dojo 0.27.0 |
| `EXTRACT_CLASS_ALIASES` maps merger to contract | Fixed (empty) | Keep; guard test |
| Field maps must match dojo (#235 acc. 1-2) | Met in mailroom, hub and sandbox vendor, all five classes | Keep; CI parity check |
| Dojo prompts request retired keys | Fixed: only in "Do not emit" lines | Keep; separate check of frozen templates vs live `contracts_specialist_v33` |
| Per-class schema + F1/F2 report (#235 acc. 3) | Open | `audit-report` (§7) |
| Archivist drops `judge_score`; no scoring block, `nodes_visited`, `pipeline_success` | Still true | §4, §5 |
| Per-stage rows; `verify_chain` per document | Still true | §4-§6 |
| No intake node / no classification review agent | Stale: `intake`, `review_classify`, `classification_arbiter` exist | Keep |
| Arbiter before judge on out-of-band extraction | Superseded by KANBAN-063 | Keep current order; update docs and issue text |
| Report, then deterministic scoring, then archive | Open | scoring node family (§3.1, §5.1) |
| Twice-failed extraction cleared by a person goes to the reporter | Not implemented | §5.4 |
| Every path writes the audit log | Open: `failed` goes to END with no row | `audit_failed` node (§5) |

## 3. Scoring: one function, owned by the dojo

Today three code paths score an extraction: the dojo's `score_archive_block` (field map
only), the sandbox's `eval/scoring.py:score_extraction_row` (scoping, subclass-aware suite,
MAUD/CUAD headlines, `unlabeled_fields`, `entity_list_f1`), and eval-environment's copy of
the scoping. The sandbox path is the most complete.

**Dojo 0.27.0** (additive; no existing name changes):

- `score_document(doc_type, predicted, expected, *, doc_text=None, doc_subclass=None,
  expected_subclass=None) -> dict` returns exactly the payload the sandbox's
  `score_extraction_row` returns today (same keys, same values). It contains the Hub
  scoping (`extraction_scope`), the detailed subclass-aware suite path, MAUD/CUAD headline
  replacement with `suite_overall_extraction_score` kept, `unlabeled_fields`, and the PRF
  keys (`extraction_precision`, `extraction_recall`, `extraction_f1`, `extraction_f2`,
  `entity_list_f1`).
- `assess_extraction_payload(predicted, doc_type, doc_subclass=None) -> dict` moves from the
  sandbox's `eval/schema_adherence.py` unchanged: `parse_error`, `schema_valid` (pydantic),
  `schema_adherence`, `parse_failure_reason`, `extra_schema_keys`, `legacy_schema_keys`,
  `prose_wrapper`, `recovered_from_raw`. Pydantic models are passed in by the caller
  (`schema_lookup=`), so the dojo does not import mailroom.
- `score_archive_block(...)` gains keyword-only `doc_subclass=` and `doc_text=` and builds its
  block from `score_document`. Existing keys keep their meaning, including `status` /
  `reason` (`unscorable` / `gt_missing` with no ground truth) and the dojo's `schema_valid`
  (every live-map key present). New keys: `scoring_method`, `suite_overall_extraction_score`,
  `unlabeled_fields`, `entity_list_f1`, MAUD/CUAD fields when present.

The sandbox's `score_extraction_row` becomes a thin wrapper over `score_document`. A golden
test on recorded sandbox rows proves its output is byte-identical.

### 3.1 Tiered scorers, one per document type

`score_document` does not hold type-specific code. It is a dispatcher over three tiers, so
each document type's rules live in their own module and adding a type touches one file
plus one registry line.

| Tier | Runs for | Lives in (dojo) | Does |
|---|---|---|---|
| 0, common | every document | `scoring/doc_scorers/common.py` | Hub scoping, `assess_extraction_payload`, field-map suite score, PRF keys, `unlabeled_fields`, `status`/`reason` (`unscorable` / `gt_missing`) |
| 1, type | one document type | `scoring/doc_scorers/<doc_type>.py` | that type's extras and headline rule |
| 2, subclass | one subclass within a type | a hook inside the type's module | subclass normalisation, subclass-specific fields or alignment |

Each type module implements one small protocol:

```python
class DocScorer(Protocol):
    doc_type: str
    subclasses: tuple[str, ...]          # from DOC_TYPE_SUBCLASSES
    def subclass(self, raw: Any) -> str | None: ...                  # tier 2: normalise
    def extras(self, predicted, raw_expected, *, subclass, doc_text) -> dict: ...
    def headline(self, common: dict, extras: dict) -> tuple[float | None, str]: ...
```

`DOC_SCORERS: dict[str, DocScorer]` is the registry; `get_doc_scorer(doc_type)` returns the
type's scorer or `generic`.

The modules reproduce today's numbers exactly (golden parity, §8); they move code, they
do not change rules:

| Module | Subclasses (count) | Tier 1 extras and headline | Tier 2 |
|---|---|---|---|
| `contract` | CUAD families (25) | CUAD clause presence (`score_sandbox_cuad`) when `cuad_clause_labels` exist; headline `cuad_presence_f1`, method `+cuad` | subtype normalisation |
| `merger_agreement` | MAUD consideration types (5) | MAUD answers (`score_sandbox_maud`) when `maud_clause_labels` exist; headline `maud_accuracy`, method `+maud` | consideration type |
| `corporate_record` | record types (11) | always the detailed suite path | per-record-type field alignment (authoritative even with no subclass) |
| `correspondence` | communication forms (9) | topic and sentiment (`score_correspondence_content`) | form normalisation |
| `insurance_claim` | CMS tables and LOB lines (6) | `score_claims_extras` (determination consistency, amount exactness), canonical `claim_type` | `cms_source_table` for CMS rows; CMS documented-absent fields |
| `generic` | none | field-map score only | none |

`generic` covers the native classes with no corpus rows (`due_diligence`,
`compliance_filing`, `court_opinion`) and any unknown type, so every document is scored
or marked `unscorable` and none falls through. Where headline replacement applies,
`suite_overall_extraction_score` keeps the field-map score.

The scoring block gains `scorer: {doc_type, module, subclass, tiers_run}` so the row says
which code produced the numbers.

The 1,740-line `ScoringSuite` is not split in 0.27.0: tier 0 calls it as it is. Moving its
type-specific branches (`self.name == "insurance_claims_specialist"` and the like) into the
type modules is the 0.28.0 dojo refactor (Project B), which this registry prepares for.

**Three `schema_valid` meanings, kept apart by name:**

| Where | Meaning |
|---|---|
| `extraction.adherence.schema_valid` | payload validates against the pydantic model |
| `extraction.adherence.schema_adherence` | parsed and valid (1.0 / 0.0) |
| `scoring.schema_valid` | every live field-map key is present (dojo) |

Mailroom's `validate_extraction` and `success_rate` read `extraction.adherence`.

## 4. Data model (llm-mailroom)

All hashes are hash version 2: SHA-256 of canonical JSON (`sort_keys`) over `hash_version`,
`prev_hash`, `doc_id`, `entry_id`, `matter_id`, `actor`, `timestamp`, `event`, `detail`,
byte-identical with the dojo's `format_audit_entry`. Sub-rows and addenda use the same
function with their own id in `entry_id` and their kind in `event`.

### 4.1 `audit_log`: one fixed row per document

Columns unchanged: `entry_id`, `doc_id`, `matter_id`, `seq`, `event`, `actor`, `timestamp`,
`prev_hash`, `entry_hash`, `detail`.

- `doc_id` is **unique**.
- `seq` is one sequence across the database (it is not hashed).
- `event`: `archived`, `failed`, `triage_archived`, `triage_reviewed`.
  `actor`: `archivist`, `pipeline`, `triage`.
- `prev_hash` is the previous row's `entry_hash` in `seq` order; `""` for the first row.
- The row is never updated after it is written.

`detail` (`row_schema: 1`):

| Key | Contents |
|---|---|
| `stage`, `pipeline_success`, `failure_class`, `error_message`, `escalation_reason`, `run_attempt` | outcome |
| `review_decision`, `arbiter_decision`, `classification_arbiter_decision` | decisions |
| `doc_type`, `doc_subclass`, `classification_confidence`, `classification_attempts` | classification |
| `extraction` | `extracted_data` (final), `extraction_source` (`model` or `human`), `extraction_confidence`, `extraction_attempts`, `adherence` (§3), `extraction_gate`, `extraction_guardrail`, specialist, model, prompt version |
| `judge_verdict`, `judge_score`, `judge_findings` | LLM judge |
| `scoring` | dojo `score_archive_block` output (§3), plus `scored_payload`: `model` or `human_corrected` |
| `metrics` | exactly what `emit_pipeline_scores` returns today, plus `subclass_correct` when ground truth has a subclass |
| `nodes_visited` | ordered node names |
| `trace` | node visits (§4.4) |
| `trace_mode`, `trace_id` | `decisions` or `full`; the run's trace id |
| `attempts` | `[{attempt_id, stage, attempt_no, entry_hash}]` for every sub-row (§4.2) |
| `refs` | `report_path`, `archive_path`, `manifest_path`, `sidecar_path`, `file_sha256` |
| `recovered_from` | only on rows written by `audit-reconcile` (§5.6) |

Fields not collected on a run are `null`, never filled in.

### 4.2 `audit_attempts`: sub-rows

`attempt_id`, `parent_id` (the row's `entry_id`, or a `resumed` addendum's id), `doc_id`,
`stage` (`classification`, `extraction`, `judge`), `attempt_no`, `trigger` (`initial`,
`retry`, `arbiter_rescue`, `human_correction`, `transient`, `resume`), `outcome`,
`started_at`, `ended_at`, `detail`, `entry_hash`.

`detail` holds that attempt's full output: classification attempts carry type, subtype,
confidence and decisions; extraction attempts carry `extracted_data`, `adherence` and the
attempt's own scoring block when ground truth exists; every attempt carries its node visits,
tokens and cost. A `human_correction` attempt carries the field-level diff from the model's
last extraction, the reviewer and the time.

Sub-rows are written in the same transaction as their parent. The parent's
`detail.attempts` lists each sub-row's `entry_hash`, so the parent's hash covers them.

### 4.3 `audit_addenda`: after the row

`addendum_id`, `doc_id` (must have a row), `kind` (`rescore`, `resumed`, `review_decision`,
`relations_linked`), `seq` (per document), `actor`, `timestamp`, `detail`, `prev_hash`,
`entry_hash`. The first addendum links to the row's `entry_hash`; each later one to the
previous addendum. A `rescore` carries a new `scoring` block; a `resumed` addendum carries its
own `trace`, `scoring`, outcome and sub-rows.

### 4.4 Node visit record (`detail.trace[]`)

`visit_id`, `node`, `attempt_id`, `started_at`, `ended_at`, `status` (`done`, `error`,
`paused`), `outcome` (`ok`, `retry`, `reroute`, `fail`), `model`, `prompt_version`,
`prompt_tokens`, `completion_tokens`, `cost_usd`, `latency_ms`, `decisions`, `decision`,
`trace_sink`, `span_id`, `also_in`, `io_sha256`; plus `prompt` and `completion` when
`full_trace` is on.

- `decisions` uses the sandbox's schema exactly (`job/node_trace.py`):
  `{question, type: noul|choice|score, answer, p, expected, options, source}`. A sandbox
  contract test keeps the two in step.
- `decision` holds the node's state fields from one per-node list in `src/audit/trace.py`:
  type/subtype/confidence, reasoning text, findings, guardrail flags, extracted fields, and
  for `intake` the ModernBERT `bert_triage` result (type, subclass, confidence, fast path).
- `io_sha256` is SHA-256 of the call's full prompt and completion.

### 4.5 Legacy and metadata

- `audit_log_legacy`: today's table and rows, unchanged.
- `audit_meta`: `{migrated_at, legacy_rows, legacy_tail_hashes_sha256}`.
- `audit_trace_io` (sql sink, §5.5): `visit_id`, `doc_id`, `prompt`, `completion`, `sha256`,
  `created_at`.

## 5. Write path

### 5.1 Graph

- `compile_report` -> **scoring node family** -> `catalog_write` -> `archive`. It follows the
  dojo tiers in §3.1:
  - **`score_common`** runs tier 0 for every document and stores the result in
    `state["scoring_common"]`. It then routes on the final `doc_type` (the human-corrected
    type when a person changed it) using a conditional edge.
  - There is one **`score_<doc_type>`** node per registry entry: `score_contract`,
    `score_merger_agreement`, `score_corporate_record`, `score_correspondence`,
    `score_insurance_claim` and `score_generic`. Each runs tiers 1 and 2 through
    `get_doc_scorer(doc_type)`, then writes `state["scoring"]` as the block from
    `score_archive_block(..., doc_subclass=..., doc_text=...,
    taxonomy_bytes=<src/config/taxonomy.yaml>)`, so `field_map_sha` records the map that
    was actually used.
  - The nodes are built from `DOC_SCORERS` by one factory in `src/graph/scoring_nodes.py`.
    Each node is a thin wrapper. All scoring logic stays in the dojo.
  - Every node in the family has its own trace visit. The row's `nodes_visited` and
    `trace` show which scorer ran, and a failure in one type's scorer shows up as that node.
  - When there is no ground truth, the type node still runs and records adherence with
    `status: unscorable`.
- New **`audit_failed`** node is the target of every stop edge (`human_review` -> `failed`,
  judge stop, intake failure, rejected review) and writes the `failed` row.
- Everything else in the graph is unchanged.

### 5.2 Collecting the trace

The `traced_node` wrapper appends one visit per node to `state["audit_trace"]` (append-only
reducer). Attempt boundaries come from the existing counters (`classification_attempts`,
`extraction_attempts`, `arbiter_retry_count`, `transient_retries_*`, `run_attempt`).
LangGraph's checkpointer persists the trace across a human-tray pause or a crash. The seven
`_emit_stage_audit` calls become trace appends and no longer write rows.

### 5.3 Terminal writers

- `archive` writes the `archived` row and its sub-rows.
- `audit_failed` writes the `failed` row.
- The watcher's abort path writes `failed` from the last checkpoint.
- The triage lane writes one `triage_archived` / `triage_reviewed` row.
- `write_terminal_row` takes one global `audit_log:chain` lock, reads the tail by `seq`,
  links, and inserts the row and sub-rows in one transaction. If `doc_id` already has a row,
  it writes a `resumed` addendum instead.
- `write_addendum` (per-document lock) is used by the review API, relations step and
  rescore CLI.

### 5.4 Human review of a twice-failed extraction

| Review action (existing API) | Path | Recorded as |
|---|---|---|
| `disposition=complete`, no `extracted_data` (approve as is) | `compile_report` -> scoring nodes -> `archive` | `pipeline_success: false`, `extraction_source: model` |
| `disposition=complete` with `extracted_data` (person corrects the fields) | same path with the person's fields; no new extraction | `extraction_source: human`; `human_correction` sub-row with the diff; `scoring.scored_payload: human_corrected` |
| `disposition=resume` (re-run with guidance, or doc type / subtype corrected) | back to `extract` (unchanged) | new extraction attempt |
| `record`, `requeue` | unchanged | `review_decision` addendum |

Today `complete` archives the file directly and skips the report, scoring and audit row;
it now resumes the graph at `compile_report`.

Model performance is never credited with a person's correction: `audit_row_metrics` (§7)
reports model scores from the last model extraction attempt and the final scores separately.

### 5.5 Trace sinks (backend-agnostic)

`TraceSink` protocol in `src/audit/sinks/`: `record_io`, `capture_pending`,
`fetch_pending`, `fetch_io`.

- **`sql` (default, always on):** full I/O in `audit_trace_io`; pending rows in a separate
  `audit_spool.db` file (on the Modal volume when mounted).
- **`phoenix` (automatic when usable):** added when the Phoenix/OpenTelemetry packages
  import, `PHOENIX_TRACING` is not disabled, `resolve_provider_name`'s Railway rule allows
  it, and a 1-second probe of `PHOENIX_ENDPOINT` succeeds. I/O becomes span attributes;
  pending rows become `audit.pending_row` spans.
- **`langfuse`, `braintrust`, `otlp`:** adapters, enabled by
  `MAILROOM_TRACE_SINKS=sql,phoenix,langfuse` or registered through the
  `mailroom.trace_sinks` entry point.
- Sinks are additive (independent of the single active tracing provider). The visit records
  the first sink that accepted the I/O as `trace_sink`, others in `also_in`.
- `audit.full_trace` / `MAILROOM_AUDIT_FULL_TRACE` (default off) also copies prompt and
  completion into the row. Document text and personal data then land in the audit DB and
  rows can reach hundreds of KB.

### 5.6 Write failure fallback

After 3 failed attempts to commit the row:

1. Send the complete pending record (row `detail`, sub-rows, `detail_sha256`, sub-row hashes,
   `doc_id`, `matter_id`, `event`, `actor`, `captured_at`) to every enabled sink's
   `capture_pending`. If the payload exceeds a sink's limit, send the decisions-only version
   and the SHA-256 of the full one.
2. Set `audit_written: "pending"` and the sink references on the manifest; log
   `audit_pending`. The run still succeeds.
3. `audit-reconcile` (mailroom script; also exposed by the sandbox CLI) reads all sinks,
   deduplicates by `doc_id` and `detail_sha256`, refuses anything whose hash does not match,
   and writes each as a normal row with `detail.recovered_from` =
   `{sink, ref, captured_at, detail_sha256}`. It skips documents that already have a row.
4. If no sink accepts the record, log `audit_gap` at error level.

The sandbox's vendored graph writes rows into the sandbox's own local DB like any other
deployment; the sandbox's reports do not read them.

## 6. Verification and migration

`verify_chain` runs:

1. **Main chain:** in `seq` order, `prev_hash` links, hashes recompute, `doc_id` unique;
   recovered rows' `detail_sha256` matches `detail` without `recovered_from`.
2. **Sub-rows:** every sub-row hash matches its parent's `detail.attempts`; missing, extra or
   edited sub-rows fail.
3. **Addenda:** per document, linked from the row and from each other; their sub-rows checked
   as in 2.
4. **Legacy:** `verify_legacy_chain` (today's per-document check) on `audit_log_legacy`.
5. **Trace I/O:** `io_sha256` against the `sql` sink always; external sinks only with
   `--check-sinks`.

Migration (one-time, in `ensure_schema`, safe to re-run): back up the DB file; create the new
tables; copy every `audit_log` row byte-for-byte into `audit_log_legacy`; check counts and
hashes before emptying `audit_log`; add the unique `doc_id` index and the global `seq`; write
`audit_meta`.

`verify_audit_chains.py` reports main chain, sub-rows, addenda, legacy and pending
(manifests with `audit_written: pending` and sink pending records with no row). It exits
non-zero on any failure.

## 7. Readers, warehouse, reports

- **`GET /audit/{doc_id}`:** `{doc_id, chain_length, chain_valid, entries, legacy, pending,
  warehouse}`. `entries` in order: row, its sub-rows, each addendum then its sub-rows; each
  with `kind` (`row`, `attempt`, `addendum`) and `parent_id`. `warehouse` is
  `{export_date, files}` from the warehouse manifest.
- **`GET /audit`** and **`analyze_audit_db.py`:** rows by event and outcome, addenda by kind,
  attempts per stage, retry and rescue rates, pending and recovered counts, legacy summary,
  results of the five checks.
- **`audit_row_metrics(row, addenda, attempts) -> dict`:** one flat record per document:
  `outcome`, `pipeline_success`, `first_pass`, `doc_type`, `doc_subclass`, `class_correct`,
  `subclass_correct`, `overall_extraction_score`, `suite_overall_extraction_score`,
  `extraction_f1`, `extraction_f2`, `schema_adherence`, `scoring_status`, model-only
  counterparts (`model_overall_extraction_score`, `model_extraction_f1`, ...) from the last
  model attempt, `classification_attempts`, `extraction_attempts`, `arbiter_rescued`,
  `prompt_tokens`, `completion_tokens`, `cost_usd`, `latency_ms`, `reviewed`,
  `human_corrected`, `rescored`, `resumed`, `recovered`. The latest `rescore` supplies the
  scores and a `resumed` addendum the outcome.
- **Warehouse (#240/#241):** daily Parquet, merged by key as today: `documents_<date>`
  (unchanged), `audit_rows_<date>` (row columns, `detail` as JSON, `audit_row_metrics`
  columns; key `entry_id`), `audit_attempts_<date>` (`attempt_id`, `parent_id`),
  `audit_addenda_<date>` (`addendum_id`, `doc_id`), `audit_legacy.parquet` (once). The
  warehouse manifest maps `doc_id` to `entry_hash` and file names.
- **`audit-report`:** Markdown per live document type: unique schema, n, model, route (API or
  Modal), then F1, F2 and every other collected metric; uncollected metrics are labelled,
  never filled. Reads the audit DB or the Parquet files. This is #235 acceptance 3.
- **Langfuse scores:** `emit_pipeline_scores` is unchanged. When Langfuse is configured the
  scoring block's `overall_score`, `extraction_f1`, `extraction_f2` and `schema_adherence`
  are also emitted under their registered names.

## 8. Testing

- **Dojo:** golden parity of `score_document` and `assess_extraction_payload` against
  recorded sandbox payloads, per document type module; every `CORPUS_DOC_TYPES` and
  `NATIVE_DOC_TYPES` entry resolves to a scorer (unknown -> `generic`); every subclass in
  `DOC_TYPE_SUBCLASSES` and every `CORPUS_SUBCLASS_SURFACES` value normalises through its
  type module; each module is tested on its own; `score_archive_block` for all five classes including merger
  with MAUD, contract with CUAD, corporate subclass alignment, and `unscorable` without
  ground truth; guard tests that `gt_metadata` and `corporate_record_*` (used by
  Mailroom-Corpus-EDA) are unchanged; full suite and ruff.
- **Mailroom:** the scoring node family has one node per `DOC_SCORERS` entry and routes each
  of the five corpus types, a corpus-absent type and an unknown type to the right node; a
  human-corrected doc type routes by the corrected type;
  success fixture writes one row with a trace from `intake` to `archive`; judge
  stop and tray park each write one `failed` row naming the stopping node; resume writes a
  `resumed` addendum; rescore writes a `rescore` addendum and no second row; each §5.4 review
  action; sub-row tamper and deletion fail verification; hash parity with the dojo's
  `format_audit_entry`; migration on a copy of a legacy DB; concurrent documents do not fork
  the chain; fallback end to end (write fails, spool, reconcile, recovered row verifies);
  Phoenix sink against a fake collector; `full_trace` on and off; Parquet round trip;
  `/audit` and `/audit/{doc_id}` contract tests; golden `audit-report` output; CI field-map
  parity with the dojo.
- **Sandbox:** vendor refresh; golden test that its outputs are unchanged on recorded rows;
  decision-schema contract test; `sandbox pilot --mock` writes local audit rows; coverage
  at or above 77.
- **Hub:** `sync_packages.py`, `taxonomy_parity.py`, hub suite.

## 9. Rollout

0. Merge llm-dojo-scoring PR #113; publish v0.26.0.
1. Dojo **0.27.0**: §3 (additive). The cleanup refactor moves to 0.28.0.
2. Mailroom: pin dojo 0.27.0 (superseding the unmerged `claude/dojo-v0.26.0-pin` branch);
   §4-§7; `docs/architecture.md` updated to the current graph plus the scoring node family and
   `audit_failed`.
3. Sandbox: refresh `vendor/` (mailroom and dojo), thin wrapper, golden parity.
4. Hub: sync (#240) and warehouse readers (#241); the hub's dojo copy jumps from 0.24.2.
5. Follow-ups: The-Mailroom catch-up (`AuditChain.tsx` reads `event`/`entry_hash`/`entry_id`
   and nests sub-rows; stations for the scoring nodes and `audit_failed`); eval-environment
   scoping retirement.
6. Issues: staleness notes (§2) on #235-#239; roadmap issue in llm-mailroom linking this
   spec and the plan.

Each step lands only with its repo's full suite and lint passing.
