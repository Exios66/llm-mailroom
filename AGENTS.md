# AGENTS.md

Mailroom: a LangGraph state machine that processes legal documents through specialist LLM agents (classify → extract → report → archive) with filesystem bins, a SQLite catalog/audit log, and optional Langfuse/Braintrust tracing. Python 3.11+, no build step.

## Commands

```bash
pip install -e ".[dev]"        # install (deps NOT vendored; no venv in repo)
docker compose -f docker/docker-compose.yml up -d postgres clickhouse langfuse-server   # OPTIONAL: only for Langfuse tracing
python pipeline/watcher.py     # filesystem watcher — the main entrypoint
python api/main.py             # FastAPI on :8000
python pipeline/ops_monitor.py # scheduled Boss sweep (optional)
python cutover.py --list       # show agent→provider/model; also --recommend, --validate --agent <name>
python scripts/prepare_samples.py          # build the pilot PDF set into data/samples/
python scripts/run_pilot.py --mock         # pilot-test pipeline machinery (fake LLM)
python scripts/run_pilot.py --real         # pilot-test with real LLM (needs OPENROUTER_API_KEY)
python scripts/run_pilot.py --real --scores  # also ingest ground-truth scores to Langfuse
python scripts/run_quality_judges.py --real  # LLM-as-a-judge: classification/completeness/correctness (also --mock)
python scripts/sync_prompts.py             # push agent prompts into Langfuse prompt management (idempotent)
python scripts/sync_dataset.py             # build the mailroom-pilot Langfuse dataset (PDF text + manifest ground truth/metadata)
python scripts/sync_evaluators.py          # create the LLM-as-a-Judge evaluator + observation rule in Langfuse
python scripts/sync_dashboards.py          # sync the mailroom health dashboards into Langfuse (idempotent)
python scripts/sync_langfuse_logs.py       # mirror Langfuse traces (obs+scores) into data/langfuse_logs/ (--since 7d, --trace-id)
```

- Tests: `pytest tests/ -v` (whole suite), `pytest tests/test_agents/ -v`, `pytest tests/test_routing.py`, `-k "sorter"` for single-agent. Coverage via `--cov=. --cov-report=html`.
- No linter, formatter, or typechecker is configured — don't invent one.
- Config is in `config/taxonomy.yaml`; copy `.env.example` → `.env`. `OPENROUTER_API_KEY` is required or `llm/client.py:get_llm` raises.

## Architecture (not obvious from filenames)

- One LangGraph run per document, 11 nodes wired in `graph/build_graph.py`. Node contract: `node(state: DocumentState) -> dict[str, Any]` returning partial state updates. Conditional edges live in `graph/routing.py`.
- LLM access ONLY via `get_llm(agent_name)` (`llm/client.py`) → `llm/providers.py`. `agent_name` must match a key under `agents:` in `taxonomy.yaml`. No agent code names a provider/model; `DEFAULT_PROVIDER` env overrides provider globally. ALL chat completions go through `llm/retry.py:retry_chat_completion` (transient-failure retry: connection errors/timeouts/429/5xx only; 4xx never) and per-agent `max_tokens` caps from `taxonomy.yaml`.
- Agent system prompts are Langfuse-managed via `llm/prompts.py:get_managed_prompt` (name `mailroom-<agent_name>`, `production` label) with the identical template in code as fallback when Langfuse is off; the sync script is `scripts/sync_prompts.py`. New/changed agent prompts must be registered in `llm/prompts.py:prompt_templates()` and synced. The `json_object` boilerplate in `agents/base.py:_call_structured` is deliberately hardcoded — it guarantees the literal token `json` in messages (Qwen/Alibaba rejects requests without it) and embeds the schema in the prompt.
- Tracing is backend-agnostic via `observability/tracing.py` (`OBSERVABILITY_PROVIDER=auto|langfuse|braintrust|none`). `get_llm` passes every OpenAI client through `instrument_client` → langfuse 4.x monkeypatches `openai` `Completions.create` at import (`langfuse.openai`), so ALL LLM calls are auto-traced with no agent changes. `pipeline/env.py:load_env()` loads `.env`; it's called in `pipeline/watcher.py`, `api/main.py`, `pipeline/ops_monitor.py`, and `llm/client.py`.
- Langfuse tracing is also structured per document (best practices): `graph/build_graph.py` wraps `run_pipeline` in `pipeline_trace` (one trace per doc, deterministic trace id from filename, `session_id=matter_id` — or an explicit run-scoped `session_id`/`run_id` for pilot runs, curated input/output) and wraps every node via `traced_node` (verb-first spans: `classify-document`, `extract-fields`, ...). The `langfuse` skill lives in `.opencode/skills/langfuse/` (from github.com/langfuse/skills) for Langfuse-specific work.
- Quality scores: `observability/scores.py` emits task-spec scores — self-evident per run (`parse_error`, `schema_valid`, `stage_completed`, `guardrail_triggered`, confidence values) and ground-truth for pilot runs (`class_correct`, `stage_correct`, `confidence_calibration_error`, `expected_field_presence`); score configs are auto-created via `ensure_score_configs()`. Offline LLM-as-a-judge (`agents/judge.py`, `scripts/run_quality_judges.py`) audits classification/completeness/correctness against the taxonomy + extraction-schema task specs; live, the pipeline-result generation has two independent Langfuse evaluations: `mailroom-pipeline-judge` gives a three-way CORRECT/PARTIAL/MISS verdict (PARTIAL = substantially correct run with limited material gaps, so partial-but-useful extractions are not flattened into MISS), while `mailroom-pipeline-quality` gives a proportional 0.0-1.0 quality score. `scripts/sync_evaluators.py` deploys both evaluators and both observation rules, each targeting the same `pipeline-result` generation; this costs two independent evaluator calls per document. Grounded runs (ground truth with `expected_fields`) skip the document text in the judge input — the input is a labeled, pretty-printed expected-fields block and the output is a cleaned schema-only extraction, cutting ~90% of judge tokens. `scripts/sync_dataset.py` mirrors the pilot samples (PDF text + manifest metadata + ground truth incl. `expected_fields`) into the `mailroom-pilot` Langfuse dataset for experiments. `scripts/sync_langfuse_logs.py` mirrors traces (with observations + scores) into `data/langfuse_logs/<run>/` for offline subagent analysis.
- Agent-output guardrails: `pipeline/guards.py` validates classification (enum + confidence range) and extraction (JSON parse + schema) deterministically after every LLM call; violations clamp confidence below the routing threshold so bad output goes to retry/review instead of continuing. `pipeline/logging.py:setup_logging()` configures structlog (level `LOG_LEVEL`, format `LOG_FORMAT=json|pretty`) in every entrypoint and script.
- `config/taxonomy.yaml` is the single source of truth: `doc_classes`, `confidence:` thresholds, per-agent model mapping, `file_extensions`. Nothing is hardcoded in code.
- Files only move through `pipeline/bins.py` helpers (`claim_file`, `move_to_*`, `save_manifest`) — never direct `os.rename`/`shutil.move` in node/agent code. Flow: inbox → `processing/<worker_id>/` → archive or review/failed.
- `agents/boss.py` is used in two places: in-graph `boss_escalation` node AND `pipeline/ops_monitor.py`. Archivist, image_extractor, pdf_transcriber are procedural, not LLM agents.
- PDFs/images are transcribed in `graph/build_graph.py:_read_file_text` via `agents/pdf_transcriber.py` / `agents/image_extractor.py`. Requires `pypdf`/`pdfplumber` (declared deps); `pdftotext` (poppler) is an optional CLI fallback. PDF transcription may invoke the LLM for long texts.
- Pilot samples: `examples/samples/` (30 PDFs + external text, manifest.csv = ground truth incl. a per-sample `expected_fields` JSON column with literal expected extraction values, `dataset` column tags source corpus) + `scripts/fetch_external_samples.py` (downloads LegalBench MAUD / Atticus CUAD / Pile of Law public-domain samples; idempotent) + `scripts/run_pilot.py` (mock/real, baseline diff, `--source`; each run gets its own Langfuse session id `pilot-<mode>-<timestamp>` and a `run_id` in trace metadata + report) + `scripts/prepare_samples.py` (generates `data/samples/`). `examples/samples/ATTRIBUTION.md` documents licenses (CUAD + MAUD are CC-BY-4.0; Pile of Law samples are public-domain US government works — the NC-SA compilation is never committed).
- Storage is **SQLite by default** (no server): `data/mailroom.db` (tables `matters`, `documents`, `audit_log`) + `data/checkpoints.db` (LangGraph checkpointer via `langgraph.checkpoint.sqlite.SqliteSaver`, requires `langgraph-checkpoint-sqlite`). `storage/db.py:ensure_schema()` auto-creates tables on first use (idempotent, thread-safe). Setting `DATABASE_URL` to a Postgres URL switches the storage engine; the checkpointer always falls back to `MemorySaver` if SQLite is unavailable.
- `storage/db.py` uses `NullPool` for SQLite because aiosqlite connections are event-loop-bound and the graph spawns loops from sync threads.

## Langfuse project configuration & tracing best practices

### Our Langfuse setup (verified Aug 2026)

- **Cloud org `Jack's Organization` → project `llm-mailroom`** on US cloud (`https://us.cloud.langfuse.com`). Credentials live in `.env` (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`). The project-scoped API keys cannot read org-level resources (`get_organization_*` → 403); org endpoints require org-scoped keys.
- **Environments**: every entrypoint declares `OBSERVABILITY_ENVIRONMENT` via `pipeline.env:default_environment()` — `live` (watcher, API, ops monitor), `pilot` (`scripts/run_pilot.py`), `misc` (sync/mirroring scripts), `mock` (when `OBSERVABILITY_PROVIDER=none`). The environment is **immutable per trace**: re-running a document reuses its deterministic trace id and keeps the first run's environment/tags (verified: the 12 pilot traces created before env wiring are stuck at `default`/`development`).
- **Trace structure** (`graph/build_graph.py:run_pipeline` → `pipeline_trace`): one trace per document named `document-pipeline`, deterministic trace id seeded from the filename (correlates with our DB/catalog), `session_id = matter_id` by default (groups all documents of a matter in the Sessions view) — pilot runs override it with a run-scoped `pilot-<mode>-<timestamp>` session plus `run_id` in `metadata` — curated input (file metadata, not raw payloads) / output (report), `metadata={pipeline, run_deadline, attempt, run_id?}`, `tags`, `environment`. Every node runs as a verb-first span (`classify-document`, `extract-fields`, …) via `traced_node`; all LLM calls are auto-traced `generation` observations with model + usage via `langfuse.openai` patching.
- **13 managed prompts** `mailroom-<agent_name>` (`production` label; current versions are verified by `scripts/sync_prompts.py`) — including the judge variants `mailroom-judge-classification` / `mailroom-judge-correctness` (every LLM call links its exact prompt version); generations carry `langfuse_prompt=` so every trace links its prompt version.
- **Model registry** (synced from `taxonomy.yaml` `cost_models:` via `scripts/sync_models.py`): `qwen/qwen3.7-flash` ($0.03/$0.13 per 1M), `deepseek/deepseek-v4-flash` ($0.05/$0.25), `deepseek/deepseek-v4-pro` ($0.435/$0.87). Prices are verified against the live OpenRouter models API. Cost gotchas: (1) generation cost is computed **at ingestion time** and read from the observation **`cost_details`** field — `usage.input_cost`/`output_cost` are always null in API v2 responses; (2) the worker caches "model not found" per model string in Redis for **24h**, so a model used *before* its registry entry exists silently costs $0 until the cache is cleared — `sync_models.py --force` (delete + create) clears it.
- **One LLM connection**: OpenRouter (adapter `openai`, base `https://openrouter.ai/api/v1`, `custom_models=[deepseek/deepseek-v4-pro]`, `with_default_models=true`) — used by the LLM-as-a-Judge evaluators.
- **24 score configs** (self-evident run scores: `parse_error`, `schema_valid`, `stage_completed`, `guardrail_triggered`, confidences, `estimated_cost_usd`, `total_tokens`, …; pilot ground truth: `class_correct`, `stage_correct`, `confidence_calibration_error`, `expected_field_presence`; judge dimensions: `classification_*`, `completeness`, `extraction_correctness`), auto-created idempotently by `ensure_score_configs()`.
- **4 datasets**: `mailroom-pilot` (12 original samples) plus per-corpus `mailroom-pilot-{atticus, legalbench, pileoflaw}` (6 each). Every item carries `expected_doc_class`, `expected_stage`, and schema-compatible literal `expected_fields` from `examples/samples/manifest.csv`; `scripts/sync_dataset.py` rejects missing or unknown field truth.
- **2 project-scope LLM-as-a-Judge evaluators**: `mailroom-pipeline-judge` (three-way CORRECT/PARTIAL/MISS — MISS reserved for wrong class/stage, contradictions, failed runs, or broad omission) and `mailroom-pipeline-quality` (proportional 0.0-1.0 quality score), each with its own observation rule (`mailroom-pipeline-rule` and `mailroom-pipeline-quality-rule`) matching the single `pipeline-result` generation emitted per document trace. They run independently: the quality score does not replace or alter the run verdict. When the caller knows the ground truth (pilot runs pass `expected_doc_class`/`expected_stage` via `run_pipeline(ground_truth=...)`), both use the actual truth; grounded input has no document text and is labeled/pretty-printed. Synced via `scripts/sync_evaluators.py`, which prunes stale mailroom evaluators/rules; the 22 `managed` template evaluators are platform-locked (403 on delete) — ignore them. The `pipeline-result` generation is **unlinked by design** (no prompt exists for it — it is the evaluator target, not an LLM call).
- **2 dashboards** synced via `scripts/sync_dashboards.py` (idempotent, definitions in version control): **Mailroom Quality — per Prompt over Time** (avg score, p95 latency, and total cost per prompt as LINE_TIME_SERIES, scoped to `environment any of [live, pilot]` so a quality decline shows up as a trend automatically) and **Production Health — Judges (Qwen & DeepSeek)** (LLM-as-a-judge throughput / P95 / P99 / errors, scoped to environment `langfuse-llm-as-a-judge`).

### Tracing best practices (see the `langfuse` skill in `.opencode/skills/langfuse/`; audit against https://langfuse.com/docs/observability/best-practices)

- **Baseline per trace**: model name on every generation, token usage, descriptive names, correct nesting and observation types (generation for LLM calls, spans for steps — never a generic `tool`/`span` where a more specific type fits), no PII/confidential data, meaningful trace input/output (what a reviewer needs at a glance — not function args).
- **Names are an API**: verb-first and stable (`classify-document`, not `classify-document-8945`); keep dynamic/run-specific values in `metadata`, never in names; never name an observation after the model (that's a separate generation attribute).
- **Tags are immutable and set at creation** — use them for dimensions known upfront (feature, run context, corpus). Anything determined after the fact (e.g. judge verdicts) goes in **scores**, not tags.
- **Metadata** carries evaluation context (ground truth), request context (doc id, matter id, attempt), and raw payloads that would clutter input/output.
- **Environments** on every trace keep test/pilot runs out of production dashboards and evaluations.
- **Sessions** (`session_id`) group multi-trace workflows; **prompt linking** shows which prompt version produced each generation.
- **Self-audit loop**: after changing any instrumentation, run the instrumented path end-to-end, fetch the trace fresh from Langfuse, and audit it against the best-practices page before calling it done.
- **Cost**: ensure the model has a registry entry (matching `taxonomy.yaml` prices) *before* first use to avoid the 24h negative-cache pitfall; read costs from `cost_details`.
- **Reasoning budgets**: reporter calls are manually constructed in `agents/reporter.py`, so agent-level `reasoning_effort` must be propagated there explicitly. The reporter is configured with `reasoning_effort: none` to reserve its completion budget for visible matter-record output; `BaseAgent` handles this automatically for other agents.

### Mandatory: classify and tag every logged run

- **Never log a trace without tags.** Every run must carry: the `mailroom` tag (always set in `run_pipeline`), a run-context tag matching its environment (`pilot`/`live`), an attempt tag (`run-<n>` for re-runs), and, for pilot/corpus runs, a source tag (`source-<corpus>` e.g. `source-atticus`). These dimensions are what make the Langfuse trace table, dashboards, and tag filters usable at all.
- Because tags are immutable and the trace id is deterministic per document, **re-runs keep the first run's tags/environment** — if a run's classification context changes, do not rely on re-runs to fix it; instead pick the tags correctly on the run that creates the trace (or use a distinct seed for a genuinely new run class).

## Config gotchas

- `pipeline/config.py:load_config` is `lru_cache`d and `pipeline/bins.py` caches config at module level. Editing `taxonomy.yaml` requires restarting the watcher/API — it will not be picked up live.
- Adding a doc class touches ~5 places, all required: `taxonomy.yaml` (`doc_classes` + `agents:`), schema + `EXTRACTION_SCHEMAS` in `schemas/documents.py`, a `BaseAgent` subclass in `agents/`, a dispatch entry in `graph/build_graph.py:_build_specialist_dispatch` (the specialist-name→function map is hardcoded to 6 names), a prompt template entry in `llm/prompts.py:prompt_templates()`, and test fixtures/tests.
- Ollama runs as a profile-gated service in docker-compose: `--profile local-llm up`.

## Testing quirks

- No real LLM calls ever run in tests. `tests/conftest.py` patches `llm.client.OpenAI` and `agents.base.BaseAgent.__init__`. For new agent tests, inject `agent.client = <mock>` + `agent.model = "test-model"` like existing tests do.
- Tests run without Docker: conftest auto-sets `OPENROUTER_API_KEY` and `MAILROOM_BASE_DIR` to a tmpdir (`temp_base_dir` fixture). E2E tests build the full graph with mocked LLM and the SQLite checkpointer.
- `asyncio_mode = "auto"` is set; graph nodes are sync. Fixtures are plain-text files in `tests/fixtures/<doc_type>/`.

## Docs duplication

- `docs/` and `wiki/` mirror each other (e.g. `docs/agents.md` == `wiki/Agents.md`; `wiki/sync-wiki.sh` pushes wiki/ to the GitHub wiki). When editing user-facing docs, keep both in sync.
- `docs/agents.md` and `wiki/Agents.md` document the pipeline's LLM agents — they are architecture docs, not coding-instruction files.
