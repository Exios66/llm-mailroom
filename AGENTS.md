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
python scripts/sync_dataset.py             # build the mailroom-pilot Langfuse dataset (PDF text + manifest metadata)
python scripts/sync_evaluators.py          # create LLM-as-a-Judge evaluators + observation rules in Langfuse
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
- Langfuse tracing is also structured per document (best practices): `graph/build_graph.py` wraps `run_pipeline` in `pipeline_trace` (one trace per doc, deterministic trace id from filename, `session_id=matter_id`, curated input/output) and wraps every node via `traced_node` (verb-first spans: `classify-document`, `extract-fields`, ...). The `langfuse` skill lives in `.opencode/skills/langfuse/` (from github.com/langfuse/skills) for Langfuse-specific work.
- Quality scores: `observability/scores.py` emits task-spec scores — self-evident per run (`parse_error`, `schema_valid`, `stage_completed`, `guardrail_triggered`, confidence values) and ground-truth for pilot runs (`class_correct`, `stage_correct`, `confidence_calibration_error`); score configs are auto-created via `ensure_score_configs()`. Offline LLM-as-a-judge (`agents/judge.py`, `scripts/run_quality_judges.py`) audits classification/completeness/correctness against the taxonomy + extraction-schema task specs; the same rubrics are configured as live LLM-as-a-Judge evaluators in the Langfuse project via `scripts/sync_evaluators.py` (evaluator family `mailroom-*-judge`, observation rules per generation name). `scripts/sync_dataset.py` mirrors the pilot samples (PDF text + manifest metadata + ground truth) into the `mailroom-pilot` Langfuse dataset for experiments. `scripts/sync_langfuse_logs.py` mirrors traces (with observations + scores) into `data/langfuse_logs/<run>/` for offline subagent analysis.
- Agent-output guardrails: `pipeline/guards.py` validates classification (enum + confidence range) and extraction (JSON parse + schema) deterministically after every LLM call; violations clamp confidence below the routing threshold so bad output goes to retry/review instead of continuing. `pipeline/logging.py:setup_logging()` configures structlog (level `LOG_LEVEL`, format `LOG_FORMAT=json|pretty`) in every entrypoint and script.
- `config/taxonomy.yaml` is the single source of truth: `doc_classes`, `confidence:` thresholds, per-agent model mapping, `file_extensions`. Nothing is hardcoded in code.
- Files only move through `pipeline/bins.py` helpers (`claim_file`, `move_to_*`, `save_manifest`) — never direct `os.rename`/`shutil.move` in node/agent code. Flow: inbox → `processing/<worker_id>/` → archive or review/failed.
- `agents/boss.py` is used in two places: in-graph `boss_escalation` node AND `pipeline/ops_monitor.py`. Archivist, image_extractor, pdf_transcriber are procedural, not LLM agents.
- PDFs/images are transcribed in `graph/build_graph.py:_read_file_text` via `agents/pdf_transcriber.py` / `agents/image_extractor.py`. Requires `pypdf`/`pdfplumber` (declared deps); `pdftotext` (poppler) is an optional CLI fallback. PDF transcription may invoke the LLM for long texts.
- Pilot samples: `examples/samples/` (12 PDFs, manifest.csv = ground truth) + `scripts/run_pilot.py` (mock/real, baseline diff) + `scripts/prepare_samples.py` (generates `data/samples/`). `examples/samples/ATTRIBUTION.md` documents licenses (CUAD contracts are CC-BY-4.0; the rest is original).
- Storage is **SQLite by default** (no server): `data/mailroom.db` (tables `matters`, `documents`, `audit_log`) + `data/checkpoints.db` (LangGraph checkpointer via `langgraph.checkpoint.sqlite.SqliteSaver`, requires `langgraph-checkpoint-sqlite`). `storage/db.py:ensure_schema()` auto-creates tables on first use (idempotent, thread-safe). Setting `DATABASE_URL` to a Postgres URL switches the storage engine; the checkpointer always falls back to `MemorySaver` if SQLite is unavailable.
- `storage/db.py` uses `NullPool` for SQLite because aiosqlite connections are event-loop-bound and the graph spawns loops from sync threads.

## Config gotchas

- `pipeline/config.py:load_config` is `lru_cache`d and `pipeline/bins.py` caches config at module level. Editing `taxonomy.yaml` requires restarting the watcher/API — it will not be picked up live.
- Adding a doc class touches ~5 places, all required: `taxonomy.yaml` (`doc_classes` + `agents:`), schema + `EXTRACTION_SCHEMAS` in `schemas/documents.py`, a `BaseAgent` subclass in `agents/`, a dispatch entry in `graph/build_graph.py:_build_specialist_dispatch` (the specialist-name→function map is hardcoded to 5 names), and test fixtures/tests.
- Ollama runs as a profile-gated service in docker-compose: `--profile local-llm up`.

## Testing quirks

- No real LLM calls ever run in tests. `tests/conftest.py` patches `llm.client.OpenAI` and `agents.base.BaseAgent.__init__`. For new agent tests, inject `agent.client = <mock>` + `agent.model = "test-model"` like existing tests do.
- Tests run without Docker: conftest auto-sets `OPENROUTER_API_KEY` and `MAILROOM_BASE_DIR` to a tmpdir (`temp_base_dir` fixture). E2E tests build the full graph with mocked LLM and the SQLite checkpointer.
- `asyncio_mode = "auto"` is set; graph nodes are sync. Fixtures are plain-text files in `tests/fixtures/<doc_type>/`.

## Docs duplication

- `docs/` and `wiki/` mirror each other (e.g. `docs/agents.md` == `wiki/Agents.md`; `wiki/sync-wiki.sh` pushes wiki/ to the GitHub wiki). When editing user-facing docs, keep both in sync.
- `docs/agents.md` and `wiki/Agents.md` document the pipeline's LLM agents — they are architecture docs, not coding-instruction files.
