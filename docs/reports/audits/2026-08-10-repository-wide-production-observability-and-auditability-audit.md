# Repository-Wide Production, Observability, and Auditability Audit

- **Date**: 2026-08-10
- **Kind**: audits (Repository audit and synthesis reports)
- **Status**: draft

## Scope

Repository-wide audit of the `llm-mailroom` pipeline (workspace `llm-mailroom/`, git `HEAD` `f7dd22e`, branch `main`, 3 commits ahead of `origin/main`) plus the **visual display console** front-end (`The-Mailroom/`, server `:8001`, FastAPI + vanilla-JS pixel-art floor). Coverage domains, per the audit request:

1. Production readiness
2. Observability
3. Auditability
4. Transparency
5. Live-run behavior
6. Visual display
7. Logging
8. Silent errors

Everything read: `pipeline/`, `graph/`, `storage/`, `agents/` (incl. `langchain_agents/`), `observability/`, `llm/`, `api/`, `schemas/`, `scripts/`, `config/taxonomy.yaml`, `pyproject.toml`, `.env.example`, `docs/`, `tests/` (49 tests), plus the entire `The-Mailroom/` console (`server/`, `web/`, `mailroom_ui/`, `scripts/`, `tests/`).

## Method

- Five parallel static-analysis passes, each domain-exhaustive, every finding verified against actual source (no assumptions; disputed/stale claims re-verified against `HEAD` before inclusion).
- **Live data evidence** (read-only): `data/mailroom.db` — `integrity_check=ok`, `journal_mode=delete`, `foreign_keys=0`, 60 audit rows / 60 documents / 30 matters; SHA-256 re-computation of every stored audit-chain hash with the app's exact `compute_audit_hash()` algorithm (58/59 chains clean, 1 verifiably broken); directory census of `data/pipeline/processing/` (60 orphaned worker dirs) and `data/archive/` (0 manifest sidecars).
- **Stale-claim quarantine**: one audit pass cited `graph/run.py:1598/1622`; `graph/run.py` does **not exist** at `HEAD` — the execution scaffold is `graph/build_graph.py` (`_execute_run` at 1434, which *does* route crashes through `_finalize_aborted` 1239-1293 to the failed bin with manifest+catalog writes). Those claims are excluded as resolved/stale and replaced by verification of the real path.
- **In-flight remediation noted**: `pipeline/logging.py` + `tests/test_logging.py` + `.env.example` carry uncommitted work (rotating file log sink, `LOG_FILE`/`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` — "audit item 10.3"). It is good work and **not yet committed**; findings below assume it lands.

Finding IDs by domain: **A-** audit/transparency/integrity, **L-** live run/ops, **O-** observability/logging, **V-** visual display console. Severity: CRITICAL / HIGH / MEDIUM / LOW. 90 findings total (6 CRITICAL, 22 HIGH, 36 MEDIUM, 26 LOW).

---

## Executive Summary

The pipeline's stated design claims — *auditability over cleverness*, *hash-chained tamper-evident audit trail*, *redundant record-keeping independent of any single tool*, *behavior never depends on observability being up* — are **partially true and partially false**, and the false halves cluster exactly where they matter for legal provenance and production operations:

- **The hash chain itself is sound in design and mostly sound in data** (58/59 stored chains recompute cleanly), but it **records almost nothing**: 60/60 live `audit_log` rows are `archived`-by-archivist events. Ingest, classify, extract, retries, guardrails, conflicts, Boss decisions, review routing, failures and aborts leave **zero audit entries** (A-1). A legal discovery request cannot be answered from the compliance record. The one broken chain in the live DB (`docX`, two entries 59 µs apart, both `prev_hash=""`) disproves the code's own "can never happen" comment (A-3).
- **Durable records are not self-sufficient**: `documents.trace_id` is empty 60/60; no run/model/prompt/cost columns exist; no file checksum is ever recorded; manifests are not archive sidecars (0 JSON under `data/archive/`). With observability off, the demanded doc → run → trace → prompt → model → cost chain is unreconstructable (A-10).
- **The API is completely unauthenticated and bound to `0.0.0.0:8000`** (L-2) — confidential legal data exposure, unlimited uploads that spend real credits (L-18), and conveyor manipulation (pause, review-approve/reject) from any network peer. This is the single most urgent production issue.
- **Crash-safety gaps strand documents invisibly** (L-1, L-19): kill between `claim_file` and terminal move orphans the file in `processing/<worker_id>/` forever; a crash during review-resume leaves an unrecoverable `review`-stage ghost. No SIGTERM handling, no in-flight reconciliation (L-6).
- **Observability failures are silent and sometimes hot-path-blocking**: a per-document 29-call synchronous score-config warm-up storm when Langfuse is down-but-hanging (O-1), a per-document blocking, exception-swallowed `flush()` (O-2), no `on_dropped` wiring, dead `register_atexit_flush` (O-3/O-7), log lines without doc/run correlation context (O-4), raw document text logged on parse failure (O-6), and 9 scripts that never call `setup_logging()` (O-5).
- **The visualization console** independently hosts its own critical display-integrity defects: the REVIEW tab renders a permanent `ReferenceError` (review.js never loaded — V-1), a v1 scores fallback that can attach *other traces'* verdicts to an envelope (V-2), metrics tiles that are always `$0.00 / 0 tok / 0 calls` (aggregation over observation-less "light" runs — V-3), naïve/aware `datetime` crashes taking down the whole snapshot (V-6), and a demo-mode flag that leaks fabricated metrics into live mode (V-10).

**Verbatim silent-error inventory**: 30+ exception-swallowing sites across the pipeline (bare `except: pass`; `except Exception: logger.exception` without state change in 6 audit-relevant writers), and `.catch(() => {})` log-and-continue in ~8 front-end refresh paths.

---

## 1. Auditability & Data Integrity (A-)

### A-1 CRITICAL — The audit log records only 2 of ~10 event classes
**Refs:** `agents/archivist.py:24-37` (sole audit producer); `api/main.py:217-244` (`review_approved`/`review_rejected`); stage writers with **no** audit write: `graph/build_graph.py:276-341` (ingest), `344-431` (classify), `610-688` (extract), `733-810` (retry), `813-863` (human_review), `866-897` (boss), `900-923` (report), `1239-1293` (abort/failure).
**Evidence (live DB):** `SELECT event, actor, count(*) FROM audit_log GROUP BY event, actor` → `archived | archivist | 60` — i.e. 60/60 rows. The two documents currently in `review` have zero audit entries.
**Impact:** An auditor cannot answer "what decisions were made about this document, and when" from the compliance record. LLM decisions (classify result, extraction, retries, guardrail triggers, Boss adjudication, review routing, failures) exist only in state + Langfuse — a cloud product that may be unavailable.
**Remediation:** Emit chained entries at ingest (with file hash), classify, extract (per attempt), guardrail triggers, Boss decisions, review routing, retries, failures/aborts — inside each `traced_node`.

### A-2 CRITICAL — Audit writes are best-effort, exception-swallowed, non-atomic with the events they record
**Refs:** `graph/build_graph.py:1094-1104` (`_write_audit_log` catch → `logger.exception` only), `1077-1091` (`_latest_audit_hash`), `1074` (10 s `_run_coro` timeout), `1028-1050` (archive sequence: move → manifest → audit → catalog as 4 separate commits); `api/main.py:161-167, 243-244` (reject path saves manifest *before* audit write; `_write_review_audit_entry` swallows).
**Impact:** A crash/SQLITE_BUSY/timeout between file-move and audit write yields an archived/reviewed document with **no audit entry and no alert**. This is silent loss of the compliance record — the exact record legal provenance depends on.
**Remediation:** Persist the terminal catalog write and audit append in one transaction (or a durable outbox with retry); alert on `audit_log_write_error`; record a write-failed marker in the manifest.

### A-3 HIGH — Hash chains break under concurrency; the "can never happen" claim is false and was observed
**Refs:** `graph/build_graph.py:1077-1082` (claim), `storage/audit_log.py:46-66` (order by timestamp only), `69-79` (fetch-latest race); `schemas/audit.py:53-66` (verify).
**Evidence (read-only re-computation):** `docX` has two `archived` entries 59 µs apart, both `prev_hash=""` — `CHAIN BREAK docX … prev_hash mismatch (expected ad9048c8e489 got '')`. Read-modify-write without locking.
**Impact:** `/audit/{doc_id}` reports `chain_valid: false`; can't distinguish tampering from a race; combined with A-19 (duplicate identities) this breaks chains on re-processing.
**Remediation:** Serialize appends per `doc_id` (per-key lock or `INSERT … SELECT last-hash` inside one transaction); add a monotonic `seq` tie-break; treat a broken chain as an alertable integrity event.

### A-4 MEDIUM — Hash payload omits `actor`, `matter_id`, `timestamp`
**Refs:** `schemas/audit.py:20-28` (`compute_audit_hash` on `{prev_hash, doc_id, entry_id, event, detail}` only).
**Impact:** Those columns are mutable without breaking the chain — exactly the "who did what when" fields that matter legally.
**Remediation:** Include them with a `HASH_VERSION` field (old rows stay verifiable).

### A-5 MEDIUM — No FKs, append-only is convention only; orphaned chains observed
**Refs:** live `PRAGMA foreign_keys = 0`; schema dump (no FK clauses); `storage/audit_log.py:11-23`, `storage/catalog.py:26-46`.
**Evidence:** `docX` audit rows exist with no `documents` row (LEFT JOIN verified).
**Remediation:** FK `audit_log.doc_id → documents.doc_id` `ON DELETE RESTRICT`; `PRAGMA foreign_keys=ON` per connection; DB triggers or read-only SQL role for the audit table.

### A-6 HIGH — `matters`/`documents` updated in place; no version history
**Refs:** `storage/catalog.py:70-100, 119-128` (upsert overwrites `stage`, `doc_type`, `extracted_data`, `scores`, `escalation_reason`, `trace_id`), `49-67` (matter upsert).
**Impact:** Prior extractions/stage beliefs unrecoverable from the DB; legal provenance requires knowing what the system believed at each point.
**Remediation:** Event-sourced `document_events` table (every transition appends), or versioned rows.

### A-7 HIGH — No file hash at ingest; none verified at archive
**Refs:** repo's only `hashlib` use is `schemas/audit.py:1,28`; `graph/build_graph.py:276-341` (ingest has no digest); `agents/archivist.py:20` (move only).
**Impact:** Post-archive substitution of a legal document is undetectable.
**Remediation:** `sha256` + size in manifest and catalog at ingest; recompute at archive; store verification in the audit `detail`; ship a verification script.

### A-8 MEDIUM — No chain-verification tooling
**Refs:** only verifier is `/audit/{doc_id}` (`api/main.py:360-388`); nothing in `scripts/` (grep-verified).
**Impact:** The broken `docX` chain persisted unnoticed until this audit.
**Remediation:** `scripts/verify_audit_chains.py` (recompute all chains, nonzero exit on breakage), wired into restore per `docs/deployment.md:253-258`.

### A-9 MEDIUM — Audit writes in isolated transactions; `ensure_schema()` per append
**Refs:** `storage/audit_log.py:26-41` (own session/commit; schema check per entry — if `ensure_schema` fails the entry is lost, swallow at A-2).
**Remediation:** Batch terminal state change + audit append; cache schema readiness.

### A-10 HIGH — Provenance chain doc → run → trace → prompt → model → cost not reconstructable end-to-end
**Refs:** `graph/build_graph.py:1506-1510` (run_id in trace metadata only), `storage/catalog.py:39` (trace_id column), `schemas/manifest.py:16-32` (no run_id/model/prompt/cost), `agents/reporter.py:86-93`.
**Evidence (live DB):** `trace_id` empty in 60/60 `documents` rows (pilot ran with tracing off). No columns exist to carry run/model/prompt/cost even when tracing is on.
**Remediation:** Add `run_id`, `model`, `prompt_version`, `cost_usd`, `latency_s` to `documents` + manifest; persist in `_execute_run` post-invoke.

### A-11 MEDIUM — Manifest is not an archive sidecar
**Refs:** `pipeline/bins.py:132-141` (manifests → `{base_dir}/manifests/<doc_id>.json`); `config/taxonomy.yaml:6-13`; docs claim "sidecar" (`docs/agents.md:234`, `docs/architecture.md:184`).
**Evidence:** `find data/archive -name "*.json"` → 0.
**Remediation:** Write a manifest copy into the archive dir; correct the docs claim.

### A-12 MEDIUM — `sync_langfuse_logs.py` mirror completeness is opt-in
**Refs:** `scripts/sync_langfuse_logs.py:102-123` (`--wait-scores` default 0.0; warns `scores_not_ready`), `133`, `149-153, 168-176` (`limit=100` pages).
**Impact:** A "complete" mirror may omit async judge scores; auditors misled.
**Remediation:** Nonzero default wait; paginate; record `scores_complete` in `index.json`.

### A-13 MEDIUM — Reports carry no provenance and exist only inside `extracted_data._report`
**Refs:** `graph/build_graph.py:900-923`, `agents/reporter.py:43-51,86-93`; verified manifest `_report` = summary + data + confidences only.
**Remediation:** Attach provenance fields; store standalone `reports/<doc_id>.json`.

### A-14 MEDIUM — Ops-monitor/Boss decisions never persisted durably
**Refs:** `pipeline/ops_monitor.py:42-55` (log-only findings; pause = write `"1"` file), `api/main.py:407-448` (`/ops/sweep` persists only the pause flag), `agents/boss.py` (`analyze_system_metrics` returns dict, unpersisted), in-graph boss writes only `review_decision` to state.
**Impact:** "Who paused ingestion, when, based on what" is unprovable after the fact.
**Remediation:** Persist each sweep `{timestamp, severity, action, findings, metric snapshot}` to a catalog table / audit entries with `actor="ops_monitor"`.

### A-15 HIGH — SQLite `delete` journal mode, no `busy_timeout`, swallowing writers
**Refs:** `storage/db.py:28-40` (NullPool, no PRAGMAs — `grep busy_timeout|journal_mode|WAL` → no matches); live `PRAGMA journal_mode=delete`. Watcher/API/ops-monitor open concurrent connections.
**Impact:** Under load, audit/catalog writes hit `SQLITE_BUSY` and are silently dropped (A-2).
**Remediation:** `PRAGMA journal_mode=WAL` + generous `busy_timeout` at connect (event listener); retry-on-busy in write helpers.

### A-16 MEDIUM — No multi-write transactions; partial states on crash
**Refs:** `storage/catalog.py:49-100` (matter/doc separate commits), `graph/build_graph.py:957-971`, archive 4-step boundary (A-2).
**Remediation:** Wrap logical units in explicit transactions.

### A-17 MEDIUM — Opt-in `checkpoints.db` unmanaged; docs drift
**Refs:** `graph/build_graph.py:46-76` (MemorySaver default; `sqlite3.connect(check_same_thread=False)` no WAL/pruning), `docs/deployment.md:56` stale.
**Remediation:** Prune/wal or delete the SqliteSaver path; fix docs.

### A-18 MEDIUM — Orphaned `processing/<worker_id>/` dirs never reclaimed
**Evidence:** 60 empty orphan dirs in `data/pipeline/processing/`; `get_stuck_documents` (`storage/catalog.py:140-157`) detects but nothing acts; no recover script in `scripts/` (grep). Interlocks with L-1.
**Remediation:** `scripts/recover_processing.py` (re-queue/fail stale claims per manifest); periodic cleanup.

### A-19 HIGH — Same filename → duplicate identities; dedup is process-local and fragile
**Refs:** `pipeline/watcher.py:55-82` (`_is_already_processed` name-only terminal-manifest scan), `36-37` (process-local `_active_files`), `api/main.py:121-127`.
**Evidence (live DB):** `ambiguous_01_mixed_memo.pdf` has two `documents` rows (both `stage=review`) for one physical file; `docX` forked chains (A-3).
**Impact:** Duplicate LLM spend, forked/broken chains, catalog pollution.
**Remediation:** Content-hash or `UNIQUE(matter_id, original_filename)` identity; DB-level dedup query.

### A-20 MEDIUM — `move_to_archive` silently overwrites same-named files
**Refs:** `pipeline/bins.py:124-129` (`shutil.move` → same-name target; POSIX rename overwrites).
**Impact:** Re-processing can destroy a previously archived legal document silently.
**Remediation:** Collision-safe names (`<stem>--<doc_id><suffix>`); never overwrite.

### A-21 LOW — File moves atomic only within one filesystem
**Refs:** all `shutil.move` in `pipeline/bins.py:77-129`.
**Remediation:** Document same-filesystem requirement or journal in-flight moves.

### A-22 LOW/MEDIUM — `DATABASE_URL=postgres` switch incomplete
**Refs:** `storage/db.py:106-118` (`asyncio.run(init_db())` inside a running loop raises), `127-150` (SQLite-only migrations silently skipped for Postgres).
**Remediation:** Sync-engine bootstrap / documented migrate step; engine-agnostic migrations.

### A-23 LOW — `data/` growth has partial controls only
**Refs:** log rotation now in-flight (A-24); archive/manifests/langfuse_logs unbounded; no cleanup tooling.
**Remediation:** `scripts/cleanup_data.py` with documented retention policies.

### A-24 (IN-FLIGHT, uncommitted) — Log retention: rotating file sink
**Refs:** working tree `pipeline/logging.py` (+`tests/test_logging.py`, `+`.env.example`): `LOG_FILE`/`LOG_MAX_BYTES` (10 MB)/`LOG_BACKUP_COUNT` (5) via `RotatingFileHandler`. Resolution of "audit item 10.3". **Not yet committed — commit as part of remediation.**

---

## 2. Live-Run & Operational Flow (L-)

### L-1 CRITICAL — Crash after `claim_file` orphans documents permanently
**Refs:** `pipeline/watcher.py:121` (claim), `pipeline/bins.py:77-82` (`shutil.move` inbox→`processing/<worker_id>/`); rescans only list inbox (`watcher.py:153-157, 179`); `_is_already_processed` terminal-only (`watcher.py:55-82`).
**Behavior:** kill (crash/SIGKILL/OOM/`docker stop`) between claim and terminal move → file stranded, invisible to `/ops/stuck` after `stale_minutes` (L-? / A-18), no reconstitution path. `_finalize_aborted` (1239-1293) handles in-process exceptions but not process death.
**Remediation:** startup/ops sweep over stale `processing/` claims → requeue or finalize-as-failed (reuse the `_existing_processing_doc_id` identity logic); graceful-shutdown handler moves in-flight files to `failed/`.

### L-2 CRITICAL — Unauthenticated API bound to `0.0.0.0:8000`
**Refs:** `api/main.py:476-478`; no auth dependency/middleware anywhere (full-file read, verified).
**Exposed:** `/upload` (spends real LLM credits), `/review/{doc_id}/resolve` (reject/approve), `/ops/sweep` (pause ingestion), `/ops/resume`, `/matters/`, `/audit/` (confidential legal extraction) — all open to any network peer.
**Remediation:** Auth token/proxy, bind `127.0.0.1` by default, rate-limit `/upload` (with L-18).

### L-3 HIGH — Already-processed idempotency keyed on filename only
**Refs:** `pipeline/watcher.py:55-82` (key `original_filename == path.name`, line 76; terminal-stage check 78), `42-47` (`_mark_active` name-keyed), `160`.
**Impact:** A corrected re-submission, same-named doc in another matter, or post-archive re-upload is silently skipped (`file_skipped_already_processed` log only).
**Remediation:** Key on `(matter_id, sha256, size)`; DB-level check; documented force-reprocess flag.

### L-4 HIGH — Ingestion pause has no TTL / auto-resume; actor+reason unrecorded
**Refs:** `pipeline/bins.py:162-165` (sentinel file), writers `pipeline/ops_monitor.py:52-55`, `api/main.py:430-437`; resume only manual (`/ops/resume`, 449-473).
**Impact:** A transient incident (or L-2 abuse) halts ingestion indefinitely; paused docs age past the stuck cutoff → false stuck alerts.
**Remediation:** `expires_at` TTL; log actor+reason; exclude paused windows from stuck reporting.

### L-5 HIGH — Stuck detection is stale and inert
**Refs:** `storage/catalog.py:140-157` (`updated_at` advances only at catalog writes — long retry storms / big-PDF transcription look "stuck" while burning LLM calls; nothing acts on stuck docs — `OpsMonitor._sweep` logs only, `pipeline/ops_monitor.py:45-55`).
**Remediation:** Progress heartbeats during long nodes; connect stuck output to an automated reclaim path with operator ack; configurable cutoff.

### L-6 HIGH — No SIGTERM/SIGINT handlers; `register_atexit_flush` is dead code
**Refs:** `observability/tracing.py:191-195` (never called — grep: docstring + tests only); zero signal handlers repo-wide; watcher handles only `KeyboardInterrupt` (`pipeline/watcher.py:217-224`).
**Impact:** Managed restarts orphan in-flight files (L-1) and drop buffered Langfuse events (O-3).
**Remediation:** Signal handlers in watcher/API/ops-monitor → stop loops → `flush()`; call `register_atexit_flush()` in every entrypoint.

### L-7 HIGH — Unbounded worker threads (one per file, no backpressure)
**Refs:** `pipeline/watcher.py:102, 155-157, 169, 182-184`; no `ThreadPoolExecutor|Semaphore` anywhere (grep).
**Impact:** File burst → unbounded concurrent LLM spend (denial-of-wallet), rate-limit storms feeding the S-3 retry lattice, thread explosion.
**Remediation:** Fixed-size pool + bounded queue; concurrency metrics.

### L-8 MEDIUM — `_infer_matter_id` filename heuristic misfires
**Refs:** `pipeline/watcher.py:131-139` — any last `_`-token that is uppercase/≤10 chars becomes the matter id (`invoice_FINAL.pdf` → matter `FINAL`).
**Impact:** Mis-grouped catalog + Langfuse sessions.
**Remediation:** Explicit matterId metadata (upload form/`.meta` sidecar); heuristic only behind a strict pattern.

### L-9 MEDIUM — Medium-confidence classifications skip the retry budget
**Refs:** `graph/routing.py:68-75` — conf in `[low, high)` (`0.70–0.95`, `taxonomy.yaml:55-56`) routes straight to review; `retry_classify` reachable only below `low` (77-79).
**Remediation:** Route the band through one `retry_classify` pass first, or document intent.

### L-10 HIGH — Boss escalation node is unguarded; provider outage fails the run
**Refs:** `graph/build_graph.py:866-897` — no `try/except`, no `is_transient_error` branch (contrast classify 375-395, extract 624-645); exception → `_execute_run` catch-all → `_finalize_aborted(…, "unexpected error")` → **failed bin**.
**Impact:** A well-classified/extracted document (Boss reached only on conflict) goes to `failed/` with no human review on a provider-outage — opposite of the Boss contract (`agents/boss.py:19-24`; default `review` at 885).
**Remediation:** Transient-error return like other nodes; on any other exception default `review_decision="review"`.

### L-11 MEDIUM — Failed/aborted runs are never LLM-judged
**Refs:** `graph/build_graph.py:1327-1372` — `pipeline-result` (the only observation the evaluator rules match) suppressed for `stage in (review, failed)` (1367-1369); output carries `run_aborted`/`error_message` fields (1392-1393) but they're never emitted for exactly those runs.
**Impact:** Failure causes never qualitatively evaluated; quality dashboards silently exclude worst outcomes.
**Remediation:** Emit `pipeline-result` for failed runs with error-focused input (or a dedicated rule).

### L-12 LOW — Conflict detection compares only archived records
**Refs:** `graph/build_graph.py:515-539, 560-607` (`stage == "archived"` filter; `break` after first diff; skips newly-present fields).
**Remediation:** Widen scope via config flag; report all differing fields; document archived-only scope.

### L-13 MEDIUM — `transient_retries` budget shared across nodes, never reset
**Refs:** `graph/routing.py:13` (`_TRANSIENT_MAX_RETRIES = 2`, hardcoded), accumulators `graph/build_graph.py:380, 455, 630, 758` on one shared state key.
**Impact:** 2 transient failures in classify exhaust the counter → first transient failure in extract routes immediately to review.
**Remediation:** Per-node counters, reset on success, config-driven max.

### L-14 LOW — Run deadline checked only at node boundaries; ingest internals unbounded
**Refs:** `graph/build_graph.py:1121-1134` (wrapper-entry check), 112-122 (`_render_doc_pages` no timeout), 176-211 (transcriber bounded only by 30 s `pdftotext` timeout).
**Remediation:** Pass deadline into render/transcribe; check inside loops.

### L-15 HIGH — Asymmetric non-transient error handling: classify fails the run, extract reviews it
**Refs:** classify `raise` after transient filtering (`graph/build_graph.py:395`, retry at 470) → failed bin; extract converts to `{"_parse_error": True, ...}` (`647-656`) → guard-clamped → retry → review.
**Impact:** Classification hard-failures — exactly the docs needing human eyes — bypass the review conveyor.
**Remediation:** Mirror extract's conversion (the empty-text fast path 346-361 already demonstrates the pattern).

### L-16 HIGH — Vendored LangChain agents bypass the retry/observability layer entirely
**Refs:** `langchain_agents/base_agent.py:96-104` — `ChatOpenAI(max_retries=3, timeout=120)`; `retry_chat_completion` imported nowhere in `langchain_agents/` (grep): the **sorter** and all **six specialists** escape (a) the json-400 marker retry, (b) `llm_retry` attempt logging, (c) deadline re-checks between internal retries (checked once pre-invoke, `base_agent.py:248/309/415`), (d) run-context propagation.
**Remediation:** Adapter around `llm.invoke` with taxonomy-driven timeout/max_tokens, shared retry semantics + json-400 handling, deadline checks, same `llm_retry` event shape.

### L-17 HIGH — Multiplicative retry cascade (≈27 calls per node worst case)
**Refs:** `llm/retry.py:20-21` (SDK retries apply first), `llm/client.py:18` (no `max_retries=0`), `retry.py:107, 113-124` (wrapper ≤3), `graph/routing.py:13, 24-32` (graph self-loop ≤2). SDK(3) × wrapper(3) × loop(3) = **27 `chat.completions.create` calls** per node; wrapper backoff capped at 30 s with 0.3 jitter (123-124) against a 3600 s run deadline.
**Impact:** Cost amplification, trace inflation, extreme tail latency during incidents; traces "10-20x inflated" (watcher.py:60-62 comment).
**Remediation:** `max_retries=0` on SDK client (single retry layer), per-node attempt cap, deadline propagation into the LangChain path.

### L-18 HIGH — `/upload` has no size cap, no rate limit, no pause awareness
**Refs:** `api/main.py:101-142` — `dest.write_bytes(content)` unguarded (130); extension-only validation (108-114); never checks `is_ingestion_paused()`.
**Impact:** Disk exhaustion; fan-out into L-7's unbounded threads → unbounded spend; uploads stack during pauses.
**Remediation:** Max size + rate limit; 503 when paused; content-type validation.

### L-19 HIGH — Resume crash strands the doc in an unrecoverable review state
**Refs:** `api/main.py:171-193, 247-295`; `graph/build_graph.py:1672-1727` (resume: `requeue_from_review` → `processing/<worker>/`, bins.py:100-103); stuck detection excludes `review` (`storage/catalog.py:153`).
**Behavior:** death between requeue and terminal archive → catalog/manifest stay `review`, file in `processing/`, re-approve 404s (file not in review bin, 185-187), stuck detection never reports it.
**Remediation:** `resuming` stage / heartbeat `updated_at`; `/ops/reclaim`; idempotent approve against the processing copy.

### L-20 MEDIUM — Global single pause file; queue sizes count raw dir entries
**Refs:** `pipeline/ops_monitor.py:30, 73-79`; `api/main.py:430-437, 449-473`.
**Impact:** No per-worker/matter granularity; `.DS_Store`/partial writes inflate `/ops/status` and Boss inputs.
**Remediation:** Pause JSON with actor+reason+expiry; filter non-document entries; unify queue stat computation.

### L-21 LOW — Unsanitized `doc_id` in manifest paths
**Refs:** `pipeline/bins.py:144-151` (`manifests_dir() / f"{doc_id}.json"`), API entry points `api/main.py:145, 297, 360`; write-bearing on reject (161-169). Mitigated by `.json` suffix + JSON-shape parse (hence LOW), but should not take unvalidated identifiers.
**Remediation:** Validate `doc_id` (`[A-Za-z0-9_-]`) at the API layer.

### L-22 HIGH — One failing sample aborts the entire pilot run
**Refs:** `scripts/run_pilot.py:837` (bare comprehension over `run_sample`), report written only after all rows (847-874); `--real` cost watchdog raises `SystemExit` (130-137, from 179-180).
**Impact:** A single exception (or the watchdog) discards all collected rows; report never written.
**Remediation:** Per-sample try/except recording `{"status": "error"}`; `finally`-written report with errors section; incremental row checkpoints.

### L-23 MEDIUM — Invalid `expected_fields` silently downgrades grounded → ungrounded
**Refs:** `scripts/run_pilot.py:435-445` (`except json.JSONDecodeError: return None`); grounding gate `graph/build_graph.py:1376, 1559`.
**Impact:** Judge input switches to ≤100k-char document text; field-scoring skipped; ground-truth scores degrade silently.
**Remediation:** Fail fast in `_validate_manifest_ground_truth` on unparseable values.

### L-24 LOW — Fixed pilot report path; concurrent runs clobber; baseline overwritten
**Refs:** `scripts/run_pilot.py:872-878`.
**Remediation:** `pilot_report_<run_id>.json`; timestamped baselines.

### L-25 MEDIUM — Judge dimension errors never fail the run; no cost watchdog
**Refs:** `scripts/run_quality_judges.py:189-216, 331-365` (errors collected, `main()` never checks; exit 0 always); no `_COST_ABORT_USD` guard for judges.
**Remediation:** Nonzero exit on any dimension error; cost cap; per-dimension error counts in summary.

### L-26 MEDIUM — Judge scores attach to the first run's immutable trace
**Refs:** `scripts/run_quality_judges.py:140` (+ `scripts/run_pilot.py:545`) — deterministic seed = filename stem, same as `run_pipeline` (`build_graph.py:1658`); trace id/tags/environment immutable after creation.
**Impact:** Re-judging merges new context into the *first* run's trace — misattribution, broken before/after comparison.
**Remediation:** Run-scoped judge seeds (`f"{stem}-judge-{run_id}"`).

### L-27 LOW — Judge re-extracts PDF text on a divergent path
**Refs:** `scripts/run_quality_judges.py:115-127` (`_extract_raw_text` direct) vs pipeline's `transcribe` path (`build_graph.py:184-193`, vision-aware).
**Remediation:** Persist the exact `doc_text` used by the run; feed that to judges.

### L-28 HIGH (verified + corrected) — Node-count and jitter corrections vs. prior audits
Verified at `HEAD`: the graph registers **10 nodes, not 11** (`graph/build_graph.py:1146-1155`); exponential backoff **does** implement jitter (`llm/retry.py:123-124`, `taxonomy.yaml:23`). Prior audit claims to the contrary are stale.

---

## 3. Observability & Logging (O-)

### O-1 HIGH — Score-config warm-up storms the document path (29 calls per doc)
**Refs:** `observability/scores.py:107-144` (`ensure_score_configs` called on first score of the process — inside the per-document run; `score_configs.get(limit=100)` + up to 29 `create()`s, no explicit timeouts).
**Behavior:** Langfuse down-but-hanging → every call burns an SDK timeout → per-document stall ≈ 30 × timeout; `_configs_ensured` stays empty → repeated for every document. Connection-refused → permanent churn + one warning per doc. **The single biggest violation of "behavior never depends on observability up."**
**Remediation:** Background warm-up at process start; sticky-but-bounded retry (per-process `last_attempt`, ≥N-min spacing); explicit `timeout=`; failure ⇒ skip for this document.

### O-2 HIGH — Every run ends with a synchronous, unbounded, silently-failing `flush()`
**Refs:** `graph/build_graph.py:1613` → `observability/tracing.py:77-93` (blocks on both Langfuse + Braintrust; `except Exception: pass`).
**Impact:** blackholed backend → every document waits out the SDK send/retry backoff; per-document flush defeats SDK batching; **crashed runs never reach the flush at all** (hard failures raise out of `_execute_run` before 1613 → in-flight observations dropped).
**Remediation:** background/timer/count-cadence flush; `finally` around the flush; bounded waits.

### O-3 HIGH — Observability failures are invisible (no `on_dropped`, swallowing flush)
**Refs:** `grep on_dropped` → 0 hits; `observability/tracing.py:77-93`; score path `observability/scores.py` enqueues into SDK threads, only logs on client-queue overflow.
**Impact:** "Pipeline healthy, observability dead" is indistinguishable from "pipeline idle"; dashboards silently go stale.
**Remediation:** `on_dropped` → warning + counters; flush success/failure counts; `last_flush_ok`/`queued` in `/ops/status` + `/health`.

### O-4 MEDIUM — `merge_contextvars` configured but nothing ever binds context
**Refs:** `pipeline/logging.py` (processor wired); `bind_contextvars`/`unbind_contextvars` appears nowhere (grep).
**Impact:** `llm_call`/`llm_retry`/`classified`/`extracted` lines carry no doc_id/matter_id/run_id/trace_id — log↔trace correlation requires manual joins.
**Remediation:** Bind `(doc_id, matter_id, run_id, trace_id)` in `run_pipeline`, unbind in `finally`.

### O-5 MEDIUM — "setup_logging() in every entrypoint/script" is false for 9 scripts
Verified absent: `calibrate_field_scoring.py`, `cutover.py`, `fetch_external_samples.py`, `fetch_full_cuad.py`, `new_report.py`, `prepare_samples.py`, `run_vision_sweep.py`, `validate_pipeline.py`, `write_pilot_report.py` — three of which execute pipeline code (run_vision_sweep, validate_pipeline, calibrate_field_scoring) → structlog's unconfigured defaults (no timestamps/levels/filtering).
**Remediation:** `load_env()` + `setup_logging()` in the pipeline-driving three; AGENTS.md note for offline tools.

### O-6 MEDIUM — Raw model output (document text) written plaintext to logs on parse failure
**Refs:** `agents/base.py` (~196-198) `raw=raw[:200]`; classification error path embeds output into `error_message` (`graph/build_graph.py`); `langchain_agents/classifier.py` (~151) `print()`s the full provider error body.
**Impact:** Document-derived content from confidential legal filings lands in local log files.
**Remediation:** Log structure only (length, first 40 chars, error class); provider error bodies to DEBUG or truncate hard. Governance: no redaction/retention/DPA note anywhere, and **no kill switch** for live-run judge evaluation — recommend env gates `LIVE_DOC_TEXT_IN_OBSERVABILITY` / `LIVE_JUDGE_ENABLED`.

### O-7 HIGH — Exit paths never flush; `register_atexit_flush` dead (see also L-6)
**Refs:** `observability/tracing.py:42-50`; no caller (grep). Tail of a batch is lost exactly when operators are watching (Ctrl-C/kill).
**Remediation:** Call from the three entrypoints; SIGTERM handlers (L-6).

### O-8 MEDIUM — Langfuse client failure caches forever
**Refs:** `observability/langfuse_setup.py:36` — any init failure degrades to `_NoopLangfuse` for the process lifetime (no retry). One transient outage at startup = whole worker window untraced, silently.
**Remediation:** Async re-init on a timer; surface degradation in `/health`.

### O-9 MEDIUM — M-7: audit/catalog writes skip on DB slowness (compliance)
**Refs:** `graph/build_graph.py:1074` (`_run_coro` 10 s cap), `catalog_upsert`/`_write_audit_log` catch → log only; `get_document_status` (`api/main.py:~316`) bare `except: pass`.
**Impact:** Document completes/archives while the hash-chained audit entry and/or catalog row are missing (A-2/A-9 interlock).
**Remediation:** Pending-write WAL with retry; distinct `AUDIT_GAP` event; API fallback logs.

### O-10 MEDIUM — `_NoopLangfuse`-class silent degradation × first-load embedding model hang
**Refs:** `observability/field_scoring.py:464` (lazy `SentenceTransformer` load), `466-485, 488-494` (failures `return None` silently); load runs inside post-invoke field scoring (`_execute_run` 1558-1599) — a multi-minute model download hangs run finalization on the router thread behind `run_deadline`; no log on embedding failure.
**Remediation:** Warm the model off-path (process start); log failures; bound the load.

### O-11 MEDIUM — LangChain-agent LLM paths: SDK-internal retries invisible, raw `print`s (L-16/O-6 interlock)
**Refs:** `langchain_agents/classifier.py`, `contracts_specialist.py` (SDK `max_retries=3`, no attempt logs).

### O-12 MEDIUM — `/health` blind spots and no `/metrics`
**Refs:** `api/main.py:89-98` (real DB ping + bounded 5 s LLM ping — good) but no disk check, no watcher liveness, no pause state; no Prometheus `/metrics`; `/ops/status` unconsumed by any dashboard.
**Remediation:** disk-free/inbox-pending counts; `ingestion_paused`; watcher heartbeat file; optional `/metrics`.

### O-13 MEDIUM — Pause is never resurfaced; alerting is log-only; Boss-failure pauses conservatively
**Refs:** `pipeline/ops_monitor.py` L-4/L-20 interlock; `logger.critical` is the only escalation; transient Boss outage → pause file with no resume.
**Remediation:** `paused_at`/`paused_reason`, staleness alert, max-pause TTL, pause state in `/health`.

### O-14 MEDIUM — Silent config fallbacks for env values
**Refs:** `pipeline/env.py`, `pipeline/logging.py:60-66` — invalid `LOG_LEVEL`/`LOG_FORMAT`/`OBSERVABILITY_ENVIRONMENT` fall back silently (the `default`/`development` env stragglers trace back to this class of typo).
**Remediation:** `logger.warning` on fallback.

### O-15 LOW — `provider=none` still requires both SDKs installed; sync scripts build their own clients
**Refs:** `observability/tracing.py` unconditional SDK imports; `pyproject.toml:29-30` hard deps; `scripts/sync_*.py` own `Langfuse(...)` lifecycles.

---

## 4. Visual Display Console (V-)

### V-1 CRITICAL — REVIEW tab permanently broken (`review.js` never loaded)
**Refs:** `The-Mailroom/web/index.html:103-110` (script not included) vs `web/js/main.js:232, 275` (`ReviewView` referenced); 30 s auto-refresh re-throws `ReferenceError` every 30 s; tab renders blank.
**Remediation:** load `web/js/review.js` in `index.html`; add a 500-line cap/`window.onerror` banner.

### V-2 CRITICAL — Cross-trace score contamination via v1 fallback
**Refs:** `The-Mailroom/mailroom_ui/langfuse_source.py:203-228` — when v3 `scores.get_many_v3` returns empty, falls back to v1 `scores.get_many` which **ignores `trace_id`** on Langfuse v4 and returns a *global page* → other traces' verdicts/confidences displayed on this trace, cached up to 60 s.
**Remediation:** Remove v1 fallback (or scope it with `name=`/config filter); treat empty-v3 as empty.

### V-3 CRITICAL — Metrics tiles always zero in live mode
**Refs:** `The-Mailroom/server/main.py:81-85` — `/api/metrics` aggregates *light* (observation-less) runs; `mailroom_ui/langfuse_source.py:331-332`; `trace_interpreter.py:422-423` → LLM CALLS / TOTAL TOKENS / TOTAL COST / P95 tiles are `0` permanently ("every number from Langfuse" banner notwithstanding); review cards and sessions also lack cost/tokens.
**Remediation:** Enrich light runs with per-trace usage/cost (batched `get_run`/observations for the window), or label tiles as unavailable.

### V-4 HIGH — Partial Langfuse failure wipes the floor while the lamp stays green
**Refs:** `The-Mailroom/server/poller.py:100-102, 118-125` (any fetch failure → `snapshot = []`, `_details` cleared); one bad trace's scores aborts the whole list (`langfuse_source.py` no per-trace try/except); `/api/health` probes only `trace.list` → can stay green while the poller path is broken.
**Remediation:** partial-failure snapshot (keep last good), STALE badge, per-trace isolation, health probe matching the poller's real path.

### V-5 HIGH — N+1 score/trace fetches at ~34 req/s; cache TTLs shorter than the poll interval
**Refs:** `langfuse_source.py:85-92, 161, 200, 227, 257, 325-332`; `poller.py:118-148` — per-trace `get_scores` + per-trace `get_run` re-reads (observations+scores) every `detail_ttl`, cache TTL 1-2 s vs 3 s poll → up to ~102-302 Langfuse API calls per poll → sustained rate-limit exposure (seed_demo.py:372-415 documents real score-burst drops).
**Remediation:** batch score fetch (filter endpoint), per-run detail cache ≥ poll interval, exponential backoff on 429.

### V-6 HIGH — Mixed aware/naive datetime crashes take down the snapshot
**Refs:** `langfuse_source.py:333`, `trace_interpreter.py:186, 358-359`, `server/main.py:104` — `updated_at or datetime.min` vs aware `run_ts` → `TypeError: can't compare offset-naive and offset-aware datetimes` → `list_recent_runs` aborts → poller clears snapshot (blank floor) + endpoints 500. Same class of bug in `history.js` bucket keys (V-15). **Note:** the earlier `compute_metrics` fix (`mailroom_ui/metrics.py:29-32`) is **incomplete** — it `replace(tzinfo=...)`-relabels instead of `astimezone`-converting (V-7).
**Remediation:** normalize to UTC at parse (`parse_dt`); convert, don't relabel.

### V-7 HIGH — Timezone boundary bug: metric windows off by the full UTC offset
**Refs:** `The-Mailroom/mailroom_ui/metrics.py:27-32` (`replace(tzinfo=...)` — relabels wall clock without converting); `since = datetime.now() - timedelta(...)` is naive-local (`main.py:149`); tests feed only naive datetimes → cannot catch it. Non-UTC server (e.g. UTC+9) drops the last 9 h of runs from the window.
**Remediation:** `astimezone()` conversion + UTC-normalized `since`; tz-mixed test fixtures.

### V-8 HIGH — `.env` knobs read before `load_dotenv()`; documented knobs are dead
**Refs:** `server/main.py:27-30` (module-level env reads) vs `173-177` (`load_dotenv()`) — `MAILROOM_POLL_INTERVAL`/`RECENT_WINDOW`/`TRACE_LIMIT` never apply; `MAILROOM_TRACE_TAGS`/`MAILROOM_TRACE_ENVIRONMENTS` documented (AGENTS.md:74, `.env.example:9-15`) but read nowhere (grep).
**Remediation:** load dotenv at import top; wire the two documented knobs or remove from docs.

### V-9 HIGH — Archived/failed envelopes respawn on every snapshot
**Refs:** `web/js/floor.js:129-157, 246-265` — archived/failed runs stay in the 6 h WS payload; after slide-off the next 3 s snapshot re-creates them → perpetual pop-in/slide-off loop.
**Remediation:** tombstones (invisible flag set) keyed by run id.

### V-10 HIGH — Demo-mode flag never reset; fabricated metrics leak into live mode
**Refs:** `web/js/main.js:85` (sets `window.Mailroom.demoMode`, never cleared — `stopDemo()` only flips local var, line 289); `metrics.js:148` renders demo numbers whenever the flag is set; with Langfuse up, live WS snapshots overwrite the demo floor (applySnapshot gate `main.js:150`) while metrics stay demo-derived → mixed sources.
**Remediation:** single source of truth for mode; reset flag; mutually-exclusive demo/live rendering.

### V-11 MEDIUM — Detail cache 60 s + silent degraded fallback
**Refs:** `server/poller.py:133-143` — floor stage/verdict lags up to 60 s; `get_run` exceptions fall back to light payloads (zeroed verdict/cost) with no degraded marker.

### V-12 MEDIUM — ARCHIVE station never used in live mode
**Refs:** `web/js/floor.js:47-49` — `report`/`catalog`/`archive` all route to STATIONS[3] (REPORT); STATIONS[4] only used by replay.

### V-13 MEDIUM — HTTP fallback window mismatch (30 min vs 6 h)
**Refs:** `main.js:218` (`since=1800`) vs `main.py:28` (WS window 21600) — WS-down periods silently show 1/12 of the runs.

### V-14 MEDIUM — No WS heartbeat / staleness / last-updated anywhere in the SPA
**Refs:** `web/js/api.js:69-94` — a frozen floor with a green lamp is indistinguishable from live.

### V-15 MEDIUM — History buckets sorted lexicographically
**Refs:** `web/js/history.js:37-40` — string keys `"M/D HH:00"` via `localeCompare` mis-order across days (`"8/9 23:00"` after `"8/10 01:00"`); `.slice(0,12)` can drop the genuinely newest buckets.
**Remediation:** numeric/sortable keys (epoch or ISO).

### V-16 MEDIUM — Replay ignores real span durations; retries vanish
**Refs:** `web/js/floor.js:293-322, 369-397` — fixed 600/700 ms pacing regardless of actual timings; `spanToStage` lacks retry keys → retry stages dropped from the sequence whenever timings exist.

### V-17 MEDIUM — rAF loop burns CPU unconditionally
**Refs:** `web/js/floor.js:246-267` — 60 fps full-canvas (1440×480) redraw forever, even when FLOOR tab/document hidden and when idle; no DPR handling (pixel-art blur on retina).
**Remediation:** pause on hidden/blur + when envelope count is 0; `devicePixelRatio` aware scale.

### V-18 MEDIUM — Silent catch blocks across the SPA
**Refs:** `main.js:232-243, 254, 275-278` (`.catch(() => {})` + unconditional "refreshed" logs), `metrics.js:157`, `api.js:83` (WS non-JSON silently dropped, no console.error), server 503 `detail` discarded by `api.js:16-20`, plain-text 500 (non-Langfuse) with no JSON body (`main.py:45-50`).
**Remediation:** error banners, `window.onerror`, last-updated + retry counters, propagate 503 detail.

### V-19 MEDIUM — Sessions endpoint N+1 + light runs (no verdicts/tokens/cost)
**Refs:** `server/main.py:87-105` (≤50 per-session trace fetches; `interpret_trace` without scores).

### V-20 MEDIUM/LOW — Review queue serves light runs (zeros on every card); 7-day cap
**Refs:** `server/main.py:118-121`; `web/js/review.js:46`.

### V-21 LOW — 766 lines of dead sprite code
**Refs:** `web/js/sprites.js` (whole file) — hand-authored `SPRITES`/`drawSprite`/`PROPS` never referenced by floor.js (grep); contradicts AGENTS.md "craft centerpiece" claims.

### V-22 LOW — No `Cache-Control` on `index.html`
**Refs:** `server/main.py:141-143` — stale SPA after deploy.

### V-23 LOW — `/api/meta` returns module-level `DOC_CLASSES`, not `PipelineSchema.load().doc_classes`
**Refs:** `server/main.py:123-125`; `pipeline_schema.py:77-84` — `MAILROOM_TAXONOMY` override not reflected.

### V-24 LOW — Multiple sources of state truth (local booleans vs `window.Mailroom.*`)
**Refs:** `main.js:43-78, 218-225`.

### V-25 LOW — Unused params / no DPR on canvas
**Refs:** `floor.js:8, 20-24, 246-265` (`draw(t)`, `frame(t)`, `e.seed` unused).

### V-26 LOW — `/api/traces` filters post-filter a single 100-trace page
**Refs:** `server/main.py:64-67` — >100 runs → stage/env filters silently 0-out.

---

## 5. Silent-Error Inventory (condensed)

| # | Site | Failure mode | Silent? |
|---|------|-------------|---------|
| S-1 | `graph/build_graph.py:1536-1538` | catch-all logs `run_crashed`, does finalize — OK at HEAD, but `_finalize_aborted`'s own move/manifest/catalog writes still swallow (1283-1292) | Partial |
| S-2 | `_write_audit_log` 1094-1104, `_persist_scores` 1117, `_emit_pipeline_result` 1611, `field_scoring` 1598 | log-only, no state consequence | Yes |
| S-3 | `storage/audit_log.py` + `storage/catalog.py` writers | `SQLITE_BUSY` under `journal_mode=delete` → dropped records, no retry (A-15) | Yes |
| S-4 | `pipeline/watcher.py:119` | `file_skipped_already_processed` log-only skip (L-3) | Yes |
| S-5 | `api/main.py:~316` | bare `except: pass` in `get_document_status` | Yes |
| S-6 | `observability/tracing.py:77-93, 42-50` | flush swallow; no exit flush; no `on_dropped` (O-2/O-3/O-7) | Yes |
| S-7 | `observability/scores.py` warm-up | failure → infinite per-doc re-attempts, one warning each (O-1) | Partial |
| S-8 | `langchain_agents/` | SDK-internal retries invisible; `print()` of provider error body (L-16, O-6) | Yes |
| S-9 | `pipeline/ops_monitor.py` pause | pause with no resurfacing, no TTL, log-only alerting (L-4, O-13) | Yes |
| S-10 | Console: poller 118-125, `main.js` `.catch(() => {})`, `api.js:83` | blank/zeroed/stale UI with green lamp (V-4, V-14, V-18) | Yes |
| S-11 | Console: `get_scores` v1 fallback | wrong-but-plausible data — worst kind (V-2) | Yes |
| S-12 | Console: demo-mode leak | fabricated numbers in live mode (V-10) | Yes |

---

## 6. Summary Matrix

| Severity | Pipeline (A/L/O) | Console (V) | Total |
|----------|------------------|-------------|-------|
| CRITICAL | 4 (A-1, A-2, L-1, L-2) | 3 (V-1, V-2, V-3) | 7 |
| HIGH | 19 | 7 | 26 |
| MEDIUM | 27 | 10 | 37 |
| LOW | 16 | 6 | 22 |
| **Total** | **66** | **26** | **92** |

(Counts include A-24 in-flight remediation and L-28 verification note.)

## 7. Top 15 Ranked

| Rank | ID | Severity | Why |
|------|----|----------|-----|
| 1 | L-2 | CRITICAL | Unauthenticated API on `0.0.0.0:8000` — legal-data exposure + spend abuse + conveyor manipulation |
| 2 | A-1 | CRITICAL | Audit log records ~nothing (60/60 rows = `archived`); compliance record incomplete |
| 3 | L-1 | CRITICAL | Crash orphans documents invisibly (no SIGTERM/reclaim path) |
| 4 | A-2 | CRITICAL | Audit writes droppable + non-atomic — silent loss of provenance |
| 5 | V-3 | CRITICAL | Headline metrics always `$0.00 / 0 tok / 0 calls` in live mode |
| 6 | V-2 | CRITICAL | Cross-trace score contamination (wrong verdicts that *look* right) |
| 7 | V-1 | CRITICAL | REVIEW tab blank + uncaught exception every 30 s |
| 8 | L-16 | HIGH | Sorter + 6 specialists outside the retry/deadline/observability contract |
| 9 | L-17 | HIGH | ≈27 calls/node retry cascade; cost + tail-latency amplification |
| 10 | L-7 | HIGH | Unbounded threads → unbounded LLM spend |
| 11 | L-10 | HIGH | Boss outage fails good runs instead of routing to review |
| 12 | A-15 | HIGH | `journal_mode=delete`, no `busy_timeout` — contention silently drops records |
| 13 | A-19 | HIGH | Duplicate identities observed in live DB; forked chains |
| 14 | A-7 | HIGH | No file checksums anywhere — archive tampering undetectable |
| 15 | O-1 | HIGH | 29-call per-doc warm-up storm blocks the queue when Langfuse hangs |

## 8. Recommendations

### P0 (do before next live run)
1. **Auth + bind** the API loopback-by-default (`api/main.py:476-478`); rate-limit + size-cap + pause-gate `/upload` (L-2/L-18).
2. **Process-death reconciliation** for `processing/` claims + SIGTERM handlers in all three entrypoints (L-1/L-6).
3. **Remove/re-scope the v1 score fallback** and enrich metrics over full runs (V-2/V-3); load `review.js` (V-1).
4. **Take score warm-up + embedding-model load off the document path**; background + sticky-bounded (O-1/O-10); convert tz with `astimezone`, not `replace` (V-6/V-7).

### P1 (audit trail becomes legally defensible)
5. Emit chained audit entries for **every** stage transition incl. failures (A-1); transaction-atomic with catalog writes + retry-on-busy + WAL (A-2/A-15).
6. Record `sha256` at ingest/archive + `run_id`/`model`/`prompt_version`/`cost` columns (A-7/A-10); ship `scripts/verify_audit_chains.py` (A-8) and `scripts/recover_processing.py` (A-18).
7. Route LangChain agents through the shared retry/deadline contract; `max_retries=0` on the SDK client (L-16/L-17); guard the Boss node (L-10); mirror extract's failure handling in classify (L-15).
8. Wire `on_dropped` + flush health; bind log contextvars in `run_pipeline`; stop logging raw model text (O-3/O-4/O-6); fix the 9 unconfigured scripts (O-5).

### P2 (operational hygiene)
9. Pause TTL + actor/reason + `/health` surfacing (L-4/O-13); progress heartbeats for stuck detection (L-5); per-node retry budgets (L-13); pilot/judge isolation + partial-failure rescue (L-22-L-26).
10. Console: WS heartbeat + staleness badge + tombstones + hide-when-hidden rAF + sortable bucket keys + error banners (V-4/V-9/V-14/V-15/V-17/V-18).
11. FK enforcement + append-only triggers + `HASH_VERSION` (A-4/A-5); archive collision-safe naming (A-20); retention tooling (A-23); commit the rotating log sink (A-24).

## 9. Verification Notes

- All file:line references verified against `git HEAD f7dd22e` (pipeline) and the current `The-Mailroom` tree. `graph/run.py` claims from an earlier pass were **retracted** after verification and replaced with the real `graph/build_graph.py` paths.
- Live DB inspected read-only (`sqlite3 -readonly`; `PRAGMA integrity_check=ok`, `journal_mode=delete`, `foreign_keys=0`); hash chains re-computed with the shipped algorithm — 58/59 clean, `docX` broken (both entries `prev_hash=""`).
- 49/49 pipeline tests pass; console test suite passes. The front-end defect set (V-*) is not covered by either suite.
- This audit is additive to `AUDIT_SYNTHESIS_REPORT.md` (docs cross-reference audit) and `PILOT_AUDIT_REPORT.md` (trace-level pilot analysis); where prior audits claimed things contradicted by `HEAD`, this report takes `HEAD` as truth (L-28).