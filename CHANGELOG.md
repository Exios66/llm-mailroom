# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Every trace now carries the full mandatory tag taxonomy documented in `AGENTS.md`: `mailroom` + run-context tag matching the trace environment (`pilot`/`live`/`misc`/`mock`) + `run-<n>` for re-runs + `source-<corpus>` for pilot/corpus runs (`graph/build_graph.py:_execute_run`; `scripts/run_pilot.py` passes the manifest `dataset`). The corpus is also recorded in trace metadata as `source`.
- Live LLM-as-a-Judge evaluation simplified from 3 evaluators + 11 observation rules to exactly **one cumulative evaluator** (`mailroom-pipeline-judge`, numeric 0-1) + **one rule** (`mailroom-pipeline-rule`): the pipeline now emits a single `pipeline-result` generation per document trace (`graph/build_graph.py:_emit_pipeline_result`, input = truncated document text, output = curated result), so each document costs exactly **one judge call** scoring classification correctness + extraction correctness + completeness in a single pass. `scripts/sync_evaluators.py` deploys the evaluator/rule and prunes the old per-agent evaluators and rules; the offline per-dimension judges (`agents/judge.py`, `scripts/run_quality_judges.py`) are unchanged for pilot deep audits.
- Qwen model pricing verified against the live OpenRouter models API: `qwen/qwen3.7-flash` (`$0.03` / `$0.13` per 1M tokens) matches `config/taxonomy.yaml` `cost_models:` exactly, so per-token registry prices (3e-08 in / 1.3e-07 out) are confirmed accurate. The stale `qwen/qwen3.7-pro` registry entry (a phantom — no such OpenRouter model, never used by the pipeline) was deleted; the Langfuse model registry now contains exactly the `cost_models:` entries from `taxonomy.yaml`.
- Live LLM-as-a-Judge evaluators in Langfuse now run on `deepseek/deepseek-v4-flash` (via OpenRouter), matching the offline judge; evaluator versions were reset to a single clean v1 each (stale version history from repeated `--force` syncs removed).
- Langfuse project evaluator inventory cleaned: only the project-scope mailroom evaluators and their observation rules remain in use; the 22 Langfuse-managed template evaluators are platform-locked (`scope=managed`, API returns 403 on delete) and ignored.
- `contracts_specialist` `max_tokens` raised 4096 → 8192 in `config/taxonomy.yaml` (large contracts were truncating extraction JSON mid-run).

### Fixed

- Langfuse generation costs stayed null for every mailroom trace even after model registry entries were added: the Langfuse worker's Redis model-match cache holds a 24-hour "model not found" token per model string (`LANGFUSE_CACHE_MODEL_MATCH_TTL_SECONDS` defaults to 86400), and runs that used `qwen/qwen3.7-flash` before the registry entry existed had poisoned it. Re-syncing via `scripts/sync_models.py --force` (delete + create invokes Langfuse's `clearModelCacheForProject`) purged the stale token; cost inference now works and Langfuse backfilled `cost_details` for all historical generations. Verified: all 184 generations across the 12 pilot traces carry cost (~$0.063 total; price math confirmed token-exact). Note for future tooling: cost must be read from the observation `cost_details` field — `usage.input_cost`/`output_cost` are always null in API v2 responses.
- Run token budget enforced the wrong metric: `check_token_budget()` compared the `run_limits.max_total_output_tokens` cap against cumulative prompt+completion tokens, so large documents (e.g. a 52-page contract with ~28k input tokens per specialist call) tripped the 20k cap at the `compile-report` node boundary and aborted as failed. The cap now counts **completion tokens only**, matching its documented purpose (stuck/overly-verbose model guard); regression tests added in `tests/test_run_limits.py`.
- SQLite schema drift: `ensure_schema()` only created missing tables, so pre-existing databases lacked newer columns (e.g. `documents.scores`), causing `catalog_write_error`/`scores_persist_error` on every run. `storage/db.py` now diffs every model column against the live schema and adds missing ones via `ALTER TABLE ADD COLUMN` (idempotent, cached per DB URL).

## [0.2.2] - 2026-08-08

### Added

- Langfuse Prompt Management integration (`llm/prompts.py`): every agent's system prompt is now a managed prompt (`mailroom-<agent_name>`, `production` label) fetched at runtime and compiled with its variables, with the identical template shipped in code as a fallback when Langfuse is off or unreachable.
- `scripts/sync_prompts.py` — idempotent push of the local agent prompt templates into Langfuse prompt management (dry-run, force, and per-agent modes; new versions only when the template actually changed).
- `scripts/sync_langfuse_logs.py` — mirrors Langfuse traces (with nested observations, scores, and linked prompt versions) into `data/langfuse_logs/<run>/` plus an `index.json` for offline analysis.
- `scripts/run_quality_judges.py` — offline LLM-as-a-judge evaluation over a pilot run across three task-spec dimensions: classification correctness, extraction completeness, and extraction correctness; scores are attached to each sample's deterministic Langfuse trace and a calibration summary is appended to the pilot report.
- Judge agent extended with classification and correctness verdicts alongside completeness (`agents/judge.py`).
- Tests for the judge, prompt management, and score modules (`tests/test_judge.py`, `tests/test_prompts.py`, `tests/test_scores.py`).

### Changed

- All LLM agents now resolve their system prompts through `get_managed_prompt` and pass `langfuse_prompt=` on generation calls so every trace links to the exact prompt version used; `BaseAgent` supports system-prompt overrides and propagates the fetched prompt object.
- `observability/scores.py` extended with canonical score configs for the judge verdict dimensions (classification, completeness, correctness).
- README substantially expanded; `docs/agents.md`, `docs/architecture.md`, and `docs/configuration.md` updated; wiki pages (Agents, Architecture, Configuration) resynced.

### Fixed

- Operational Langfuse bugs: prompt fetch/compile/link behavior and score-config handling under the Langfuse backend.
- Trace-log mirroring quirks (e.g., the API rejecting most `order_by` formats — mirror now sorts locally).
- PDF transcription prompt handling for the managed-prompt flow.

### Removed

- `scripts/run_completeness_judge.py` (superseded by `scripts/run_quality_judges.py`).

## [0.2.1] - 2026-08-08

### Added

- Offline LLM-as-a-judge agent (`agents/judge.py`) that evaluates extraction completeness against the source document text; runs only via scripts, never inside the pipeline.
- Transient-failure retry for all LLM calls (`llm/retry.py`): `retry_chat_completion` with exponential backoff and jitter covering connection errors, timeouts, 429s, and 5xx only — client errors are never retried; every attempt is traced as its own generation.
- Quality scoring layer (`observability/scores.py`): canonical score configs auto-registered in Langfuse (idempotent), self-evident production scores emitted per document run (`parse_error`, `schema_valid`, `stage_completed`, classification/extraction confidence), ground-truth pilot scores (`class_correct`, `stage_correct`, `confidence_calibration_error`), and extraction-schema validation.
- `scripts/run_completeness_judge.py` — offline completeness audit over pilot runs (superseded in 0.2.2).
- Ground-truth score ingestion in `scripts/run_pilot.py` (`--scores` flag): attaches class/stage correctness and calibration scores to each sample's deterministic Langfuse trace; pilot report now includes extracted data per sample.
- Pipeline now emits and persists quality scores for every finished run (`graph/build_graph.py` + `storage/catalog.py:update_document_scores`).
- Config additions in `config/taxonomy.yaml`: per-agent `max_tokens` caps, `llm_retry` tunables, `pdf_direct_chars_per_page` threshold, and agent entries for `pdf_transcriber` and `judge`.
- Subagent definitions under `.opencode/agents/`: legal-changelog-auditor, mailroom-arch-optimizer, trace-log-analyst.
- Tests for base-agent behavior (`tests/test_agents/test_base.py`) and retry logic (`tests/test_llm_retry.py`).

### Changed

- All agent LLM calls now route through `retry_chat_completion` with per-agent `max_tokens` caps (default 4096).
- Structured-output boilerplate switched to the lowercase "json" wording that Qwen/Alibaba providers require verbatim — fixes JSON-mode 400 rejections.
- PDF transcription: clean text-based PDFs are extracted directly (no LLM pass); the LLM reformat pass only runs for scanned or garbled documents.
- Pilot mock mode keys canned responses off instruction content instead of JSON schema names.

### Fixed

- Async-safe persistence of pipeline quality scores into the document catalog.
- Pilot-run bugs: mock classification/confidence handling, score ingestion, and report structure.

## [0.2.0] - 2026-08-08

### Added

- Braintrust tracing backend as an alternative to Langfuse: new backend-agnostic observability facade (`observability/tracing.py`) selecting via `OBSERVABILITY_PROVIDER=auto|langfuse|braintrust|none`; OpenAI clients are auto-instrumented for whichever backend is active, so agent code never changes.
- SQLite-first storage: the default database is now a serverless SQLite file (`data/mailroom.db` — matters, documents, audit_log) auto-created on first use via `ensure_schema()`; Postgres remains available by setting `DATABASE_URL`. LangGraph checkpoints moved to SQLite (`langgraph-checkpoint-sqlite`) with a MemorySaver fallback.
- `pipeline/env.py` — a single `.env` loader shared by the watcher, API, ops monitor, LLM client, and scripts.
- Pilot assets: example legal PDFs (CUAD-derived, CC-BY-4.0) and source texts under `examples/`, a ground-truth `manifest.csv`, `ATTRIBUTION.md`, `scripts/prepare_samples.py` (including HuggingFace dataset download support), and `scripts/run_pilot.py` (mock and real-LLM pilot runs with baseline diffs).
- Langfuse skill bundle under `.opencode/skills/langfuse/`, `AGENTS.md`, and per-package READMEs.
- New runtime dependencies: `langgraph-checkpoint-sqlite`, `aiosqlite`, `pypdf`, `pdfplumber`, `reportlab`, `braintrust`.
- Tests for observability, PDF transcription, and sample manifest integrity.

### Changed

- Default LLM models switched from `openai/gpt-4o` to `qwen/qwen3.7-flash` for the sorter, specialists, and reporter, and to `deepseek/deepseek-v4-pro` for the boss (`config/taxonomy.yaml`).
- Langfuse setup expanded to structured per-document tracing: one deterministic trace per document (seeded by filename), `session_id=matter_id` grouping, and verb-first traced node spans.
- Deployment and architecture docs updated for SQLite-first storage and the Braintrust option; wiki pages resynced.

## [0.1.0] - 2026-08-08

### Added

- Initial release of the multi-agent legal document processing pipeline.
- Core pipeline: an 11-node LangGraph state machine (ingest, classify, extract, report, catalog, archive, with retry loops, BossAgent conflict adjudication, and human-review routing) with conditional routing in `graph/routing.py` and checkpointed crash recovery.
- Specialist LLM agents: `SorterAgent` classifier, five extraction specialists (contracts, corporate records, due diligence, correspondence, compliance), `BossAgent`, `ReporterAgent`, and the procedural `Archivist`.
- Image and PDF text extraction: vision-capable `ImageExtractor` and `PDFTranscriber` agents with a `pdftotext` CLI fallback, wired into the ingest stage with per-extension file handling.
- Provider-agnostic LLM layer (`llm/client.py`, `llm/providers.py`) over OpenRouter, plus a local-model cutover workflow (`cutover.py` with `--list`, `--recommend`, `--validate`).
- Filesystem bin pipeline (`inbox → processing → archive | review | failed`) via `pipeline/bins.py`; watchdog-based filesystem watcher, FastAPI service with upload/status/audit endpoints, and scheduled ops monitor.
- SQLAlchemy storage layer: document catalog, hash-chained audit log, and matter records; Docker Compose setup for Postgres, ClickHouse, Langfuse, and Ollama.
- `config/taxonomy.yaml` as the single source of truth for document classes, confidence thresholds, file extensions, and the agent-to-model mapping.
- Langfuse tracing setup for per-document traces.
- Full documentation set: README, `docs/` (agents, api, architecture, configuration, deployment, local-models, testing), wiki pages, and `wiki/sync-wiki.sh`.
- Test suite with mocked LLM: agent unit tests, routing tests, audit-log tests, and end-to-end pipeline tests, plus text fixtures for all document classes.

### Changed

- Pipeline ingest refactored for robust file-type handling with per-extension extraction (text, image, PDF, DOCX); specialist dispatch driven by the taxonomy doc-class config.
- Document catalog records are upserted on write; archive and catalog writes made async-safe.
- File moves in `pipeline/bins.py` now use `shutil.move`; default file-extension fallback added.
- Watcher and ops-monitor robustness fixes; audit log writer made async-safe; `Matter.opened_at` made timezone-aware.

[Unreleased]: https://github.com/Exios66/llm-mailroom/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/Exios66/llm-mailroom/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Exios66/llm-mailroom/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Exios66/llm-mailroom/compare/v0.1.0...v0.2.0
