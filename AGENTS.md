# AGENTS.md

Mailroom: a LangGraph state machine that processes legal documents through specialist LLM agents (classify → extract → report → archive) with filesystem bins, a SQLite catalog/audit log, and optional Langfuse/Braintrust tracing. Python 3.11+, no build step.

## Skills (all agents)

Cursor agents discover **project skills** under `.cursor/skills/` (start with
`mailroom-tool-router`, then one specialty: openrouter, ollama, modal,
langfuse, apache-phoenix, braintrust, huggingface, langgraph, dojo-scoring,
legalbench). Use those instead of inventing a parallel provider or sink.

Deeper vendored skills under `.opencode/skills/` remain available:

- **langfuse** (github.com/langfuse/skills) — CLI API access, docs,
 instrumentation/prompt-migration/prompt-engineering references, judge
 calibration, error analysis, v4 migration.
- **braintrust** (github.com/braintrustdata/braintrust-skills) — the
 agent-auto-improvement loop: production traces -> failure taxonomy ->
 remote Braintrust dataset -> scorers -> offline eval file -> iterate, then
 push online scorers. Matches this repo's eval discipline (Braintrust
 datasets, Eval loops, deterministic local scoring, experiment log).
- **openrouter-*** (github.com/OpenRouterTeam/skills) — `openrouter-models`
 (catalog, pricing, context, provider latency/uptime — grounds the
 `taxonomy.yaml` model registry + cost prices), `openrouter-generations`
 (per-request cost/latency/tokens/provider routing — debug unexpected
 generations), `openrouter-analytics` (spend/usage queries), and
 `openrouter-benchmarks` (live benchmark data for model selection).
- **langchain-*/langgraph-*** (github.com/langchain-ai/langchain-skills) —
 fundamentals, python quickstarts, dependencies, middleware,
 langgraph-fundamentals/persistence (checkpointers — SqliteSaver)/
 human-in-the-loop (review/interrupt nodes)/cli, ecosystem-primer.

Invoke the matching **project** skill (`.cursor/skills/`) before writing or
changing agent/graph/tracing/provider code; use `.opencode/skills/` for CLI
and upstream docs depth.

### Langfuse best-practices compliance (verified 2026-08-12)

The observability layer satisfies the langfuse instrumentation baseline:
model name + token usage on every generation (auto-traced via
`langfuse.openai`), descriptive stable trace name `document-pipeline`,
verb-first node spans with correct types (spans for steps, generations for
LLM calls — never a generic tool/span), PII masked via curated input/output
(file metadata, not raw payloads), `session_id` grouping (matter/run),
tag taxonomy (environment/run/source), environments on every trace,
`langfuse_prompt=` linking, auto-created score configs, native LLM-as-a-
judge evaluators, synced dashboards, flush health + `on_dropped`, and the
mandatory self-audit loop (run end-to-end, fetch the trace fresh, audit
against https://langfuse.com/docs/observability/best-practices — documented
in the Langfuse section above). Running on langfuse 4.14.3 with the v4-
compatible API surface (`create_trace_id`, `propagate_attributes`,
score-config categories).

## Commands

```bash
pip install -e ".[dev]"        # install (deps NOT vendored; no venv in repo)
docker compose -f src/config/docker/docker-compose.yml up -d postgres clickhouse langfuse-server   # OPTIONAL: only for Langfuse tracing
PYTHONPATH=src python -m pipeline.watcher   # filesystem watcher (optional when the API embeds it)
PYTHONPATH=src python -m api.main           # FastAPI on :8000 — embeds the inbox watcher by default
docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build  # reachable producer for The-Mailroom REVIEW resolve
PYTHONPATH=src python src/scripts/publish_space.py --check  # validate HF Docker Space payload (MAILROOM_PIPELINE_URL + Observatory pair)
PYTHONPATH=src python -m pipeline.ops_monitor  # scheduled Boss sweep (optional)
PYTHONPATH=src python src/scripts/cutover.py --list       # show agent→provider/model; also --recommend, --validate --agent <name>
PYTHONPATH=src python src/scripts/prepare_samples.py          # build the pilot PDF set into data/samples/
PYTHONPATH=src python src/scripts/run_pilot.py --mock         # pilot-test pipeline machinery (fake LLM, full 25-sample live-manifest set)
PYTHONPATH=src python src/scripts/run_pilot.py --real         # pilot-test with real LLM (needs OPENROUTER_API_KEY; real committed samples only)
PYTHONPATH=src python src/scripts/run_pilot.py --real --scores  # also ingest ground-truth scores to Langfuse
PYTHONPATH=src python src/scripts/run_hf_pilot.py --check       # network-free HF-pilot + intake contract (The-Mailroom orchestrator)
PYTHONPATH=src python src/scripts/run_hf_pilot.py --examples --mock  # 48 Hub class×subclass examples (docclass-pilot v5)
PYTHONPATH=src python src/scripts/run_hf_pilot.py --dataset enron --max-scan 50 --mock  # Enron correspondence corpus
PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 1  # stratified docclass-merged v5 subset → Langfuse session pilot-hf-<stamp>
PYTHONPATH=src python src/scripts/run_quality_judges.py --real  # LLM-as-a-judge: classification/completeness/correctness (also --mock)
PYTHONPATH=src python src/scripts/run_agent_eval.py --list      # per-agent isolation eval (also --agent <name>|all --mock|--real --n --self-check)
PYTHONPATH=src python src/scripts/sync_prompts.py             # push agent prompts into Langfuse prompt management (idempotent)
PYTHONPATH=src python src/scripts/sync_dataset.py             # build the mailroom-pilot Langfuse dataset (PDF text + manifest ground truth/metadata)
PYTHONPATH=src python src/scripts/sync_evaluators.py          # create the LLM-as-a-Judge evaluator + observation rule in Langfuse
PYTHONPATH=src python src/scripts/sync_dashboards.py          # sync the mailroom health dashboards into Langfuse (idempotent)
PYTHONPATH=src python src/scripts/sync_langfuse_logs.py       # mirror Langfuse traces (obs+scores) into data/langfuse_logs/ (--since 7d, --trace-id)
PYTHONPATH=src python src/scripts/run_vision_sweep.py --real --max-docs 3  # vision-vs-text tradeoff sweep (text-only/vision-10/vision-all), real or --mock
PYTHONPATH=src python src/scripts/write_pilot_report.py       # render tracked markdown+JSON pilot report (default docs/reports/pilots/pilot-vision-tradeoff.md)
PYTHONPATH=src python src/scripts/new_report.py audits "TITLE"  # scaffold a new report under docs/reports/<kind>/ (audits|pilots|evaluations)
PYTHONPATH=src python -m legalbench.cli --list-tasks      # LegalBench suite tasks (binary QA + family classification)
PYTHONPATH=src python -m legalbench.cli --task contract_qa --n 30 --model qwen/qwen3.7-flash   # real LegalBench run (Langfuse-traced)
PYTHONPATH=src python -m legalbench.cli --task family_classification --n 20 --mock             # deterministic mock baseline (no API key)
```

- Tests: `pytest -v` (whole suite), `pytesttest_agents/ -v`, `pytesttest_routing.py`, `-k "sorter"` for single-agent. Coverage via `--cov=src --cov-report=html`.
- No linter, formatter, or typechecker is configured — don't invent one.
- Config is in `config/taxonomy.yaml`; copy `.env.example` → `.env`. `OPENROUTER_API_KEY` is required or `llm/client.py:get_llm` raises.

## Architecture (not obvious from filenames)

- One LangGraph run per document, 13 nodes wired in `graph/build_graph.py` (`ingest`, `classify`, `retry_classify`, `review_classify`, `extract`, `retry_extract`, `judge_verify`, `arbiter`, `human_review`, `boss_escalation`, `compile_report`, `catalog_write`, `archive`). Node contract: `node(state: DocumentState) -> dict[str, Any]` returning partial state updates. Conditional edges live in `graph/routing.py`.
- LLM access ONLY via `get_llm(agent_name)` (`llm/client.py`) → `llm/providers.py`. `agent_name` must match a key under `agents:` in `taxonomy.yaml`. No agent code names a provider/model; `DEFAULT_PROVIDER` env overrides provider globally. ALL chat completions go through `llm/retry.py:retry_chat_completion` (transient-failure retry: connection errors/timeouts/429/5xx only; 4xx never) and per-agent `max_tokens` caps from `taxonomy.yaml`.
- Agent system prompts are Langfuse-managed via `llm/prompts.py:get_managed_prompt` (name `mailroom-<agent_name>`, `production` label) with the identical template in code as fallback when Langfuse is off; the sync script is `scripts/sync_prompts.py`. New/changed agent prompts must be registered in `llm/prompts.py:prompt_templates()` and synced. The `json_object` boilerplate in `agents/base.py:_call_structured` is deliberately hardcoded — it guarantees the literal token `json` in messages (Qwen/Alibaba rejects requests without it) and embeds the schema in the prompt.
- Tracing is backend-agnostic via `observability/tracing.py`
  (`OBSERVABILITY_PROVIDER=auto|langfuse|braintrust|phoenix|none`). The `auto`
  chain is aligned with `llm-entity-extraction`: Langfuse if its secret key is
  set, else Braintrust if its key is set, else the **local cost-free Arize
  Phoenix** backend (`src/observability/phoenix_setup.py`, OpenTelemetry-native,
  SQLite, no subscription/tokens), then `none` — so tracing never silently turns
  off. `get_llm` passes every OpenAI client through `instrument_client` →
  langfuse 4.x monkeypatches `openai` `Completions.create` at import
  (`langfuse.openai`); Phoenix instruments the OpenAI SDK via OpenInference. So
  ALL LLM calls are auto-traced with no agent changes. `pipeline/env.py:load_env()`
  loads `.env`; it's called in `pipeline/watcher.py`, `api/main.py`, `pipeline/ops_monitor.py`,
  and `llm/client.py`.
- Langfuse tracing is also structured per document (best practices): `graph/build_graph.py` wraps `run_pipeline` in `pipeline_trace` (one trace per doc, deterministic trace id from filename, `session_id=matter_id` — or an explicit run-scoped `session_id`/`run_id` for pilot runs, curated input/output) and wraps every node via `traced_node` (verb-first spans: `classify-document`, `extract-fields`, ...). The `langfuse` skill lives in `.opencode/skills/langfuse/` (from github.com/langfuse/skills) for Langfuse-specific work.
- Quality scores: `observability/scores.py` emits task-spec scores — self-evident per run (`parse_error`, `schema_valid`, `stage_completed`, `success_rate` first-pass STP with no ground truth, `guardrail_triggered`, confidence values) and ground-truth for pilot runs (`class_correct`, `stage_correct`, `confidence_calibration_error`, `expected_field_presence`); score configs are auto-created via `ensure_score_configs()`. Offline LLM-as-a-judge (`agents/judge.py`, `scripts/run_quality_judges.py`) audits classification/completeness/correctness against the taxonomy + extraction-schema task specs; live, the pipeline-result generation has two independent Langfuse evaluations: `mailroom-pipeline-judge` gives a three-way CORRECT/PARTIAL/MISS verdict (PARTIAL = substantially correct run with limited material gaps, so partial-but-useful extractions are not flattened into MISS), while `mailroom-pipeline-quality` gives a proportional 0.0-1.0 quality score. `scripts/sync_evaluators.py` deploys both evaluators and both observation rules, each targeting the same `pipeline-result` generation; this costs two independent evaluator calls per document. Grounded runs (ground truth with `expected_fields`) skip the document text in the judge input — the input is a labeled, pretty-printed expected-fields block and the output is a cleaned schema-only extraction, cutting ~90% of judge tokens. `scripts/sync_dataset.py` mirrors the pilot samples (PDF text + manifest metadata + ground truth incl. `expected_fields`) into the `mailroom-pilot` Langfuse dataset for experiments. `scripts/sync_langfuse_logs.py` mirrors traces (with observations + scores) into `data/langfuse_logs/<run>/` for offline subagent analysis.
- Deterministic field scoring (issues #4/#5): `observability/field_scoring.py` adds a cheap, backend-agnostic, field-type-aware extraction scorer (`id`/`date`/`money` exact-after-normalize, `name` Jaro-Winkler + token-set ratio, `free_text` SQuAD token F1, `entity_list` optimal bipartite matching via scipy Hungarian → precision/recall/F1) with an optional sentence-transformers embedding cosine second signal that rescues lexically-distant-but-semantically-equal fields below `field_scoring.embedding_rescue_below`. The per-doc-class `field_types:` mapping lives in `config/taxonomy.yaml` (`doc_classes[].field_types`, list elements as `entity_list:<type>`; name-based heuristic fallback for unmapped fields); `field_scoring:` configures the ambiguity band, match threshold, and embedding model. `observability/langfuse_field_scoring.py:score_and_log_extraction` attaches `extraction_field_score`/`extraction_overall_score`/`extraction_needs_judge_review`/`entity_list_precision`/`entity_list_recall` to the trace. Judge gating: for grounded runs, `graph/build_graph.py` suppresses the `pipeline-result` generation entirely when the deterministic verdict is unambiguous (outside `ambiguous_band`), saving both evaluator calls — the LLM judge only runs when a field lands in the band or there is no ground truth (live runs unchanged).
- Agent-output guardrails: `pipeline/guards.py` validates classification (enum + confidence range) and extraction (JSON parse + schema) deterministically after every LLM call; violations clamp confidence below the routing threshold so bad output goes to retry/review instead of continuing. `pipeline/logging.py:setup_logging()` configures structlog (level `LOG_LEVEL`, format `LOG_FORMAT=json|pretty`) in every entrypoint and script.
- `config/taxonomy.yaml` is the single source of truth: `doc_classes`, `confidence:` thresholds, per-agent model mapping, `file_extensions`. Nothing is hardcoded in code.
- Files only move through `pipeline/bins.py` helpers (`claim_file`, `move_to_*`, `save_manifest`) — never direct `os.rename`/`shutil.move` in node/agent code. Flow: inbox → `processing/<worker_id>/` → archive or review/failed.
  - `agents/boss.py` is used in two places: in-graph `boss_escalation` node AND `pipeline/ops_monitor.py`. Archivist, image_extractor, pdf_transcriber are procedural, not LLM agents.
- **Vision is additive (content-completeness guarantee)**: every agent prompt always contains the full `doc_text` (budget-truncated); page images are appended only for vision-capable models. `vision.max_pages` (0 = all pages) bounds the image budget, never the content. Environment overrides `MAILROOM_VISION_ENABLED`, `MAILROOM_VISION_MAX_PAGES`, `MAILROOM_VISION_DPI` let a pilot sweep configs without touching taxonomy.yaml.
- PDFs/images are transcribed in `graph/build_graph.py:_read_file_text` via `agents/pdf_transcriber.py` / `agents/image_extractor.py`. Requires `pypdf`/`pdfplumber` (declared deps); `pdftotext` (poppler) is an optional CLI fallback, and `pymupdf` (fitz) enables **vision ingestion**. **Vision mode**: PDFs are also rendered to page-image data-URIs (`graph/build_graph.py:_render_doc_pages` → `llm/vision.py`) and sent to the sorter/specialist prompts as multimodal `image_url` content whenever agent models listed under `vision:` in `taxonomy.yaml` (Qwen etc.) — see `agents/base.py:_build_multimodal`. Vision is **additive, never subtractive**: the full `doc_text` transcription is always the message body and page images are appended on top, so no page cap ever drops document content (`llm/vision.py:render_pdf_pages` with `cap<=0` renders ALL pages; the strategy config default `vision.max_pages=10` only bounds the image budget). If the pipeline is vision-capable the expensive LLM transcription pass is skipped for scanned PDFs (`llm/vision.py:pipeline_uses_vision`). `scripts/run_vision_sweep.py --real` measures the text-only vs vision-N vs vision-all tradeoff and `scripts/write_pilot_report.py` renders it to `docs/reports/pilots/pilot-vision-tradeoff.md`.
- Pilot samples: `docs/examples/samples/` (25 PDFs + external text, manifest.csv = ground truth incl. a per-sample `expected_fields` JSON column with literal expected extraction values, `dataset` column tags source corpus) + `scripts/fetch_external_samples.py` (downloads LegalBench MAUD / Atticus CUAD / Pile of Law public-domain samples; idempotent) + `scripts/run_pilot.py` (mock/real, baseline diff, `--source`; each run gets its own Langfuse session id `pilot-<mode>-<timestamp>` and a `run_id` in trace metadata + report) + `scripts/prepare_samples.py` (generates `data/samples/`). **Real (non-mock) runs are restricted to the actual committed legal documents** — the 9 Atticus/CUAD contract & agreement PDFs (`contract_01..03`, `atticus_01..06`) plus the 6 LegalBench MAUD samples (15 real samples; see `scripts/prepare_samples.py:is_real_sample`). The repo-written synthetic `.txt`-derived PDFs (compliance/corporate/correspondence/insurance/ambiguous, 10 samples) are **mock-only** — `run_pilot.py --real` and `run_quality_judges.py --real` refuse to process them (they exist only to exercise pipeline machinery; they must never spend real LLM/eval tokens or pollute live traces). The three `insurance_claim` letters (`insurance_01` approved / `insurance_02` denied / `insurance_03` partial) complement the local eval pack in `observability/local_eval_packs.py`. `docs/examples/samples/ATTRIBUTION.md` documents licenses (CUAD + MAUD are CC-BY-4.0; Pile of Law samples are public-domain US government works — the NC-SA compilation is never committed).
- Storage is **SQLite by default** (no server): `data/mailroom.db` (tables `matters`, `documents`, `audit_log`). `storage/db.py:ensure_schema()` auto-creates tables on first use (idempotent, thread-safe). Setting `DATABASE_URL` to a Postgres URL switches the storage engine. The LangGraph **checkpointer is MemorySaver by default** (`graph/build_graph.py:_build_checkpointer()`), held on a process-level compiled graph so `human_review_node` can pause with `interrupt()` and resume with `Command(resume=...)`. The filesystem review bin is the durable park across process restart; `resume_from_review` falls back to a fresh extract invoke when the checkpoint is gone. Set `MAILROOM_CHECKPOINTER=sqlite` to opt into the on-disk SqliteSaver at `data/checkpoints.db` (debugging/resume-across-restart; falls back to MemorySaver if SQLite is unavailable).
- `storage/db.py` uses `NullPool` for SQLite because aiosqlite connections are event-loop-bound and the graph spawns loops from sync threads.
- **LegalBench suite (`legalbench/`)**: a self-contained evaluation submodule (see `legalbench/README.md`) that evaluates models through the LegalBench task families on the locally-mirrored corpora (`data/cuad/`) — `contract_qa` (binary answer: the full CUAD annotations, 510 contracts × 41 categories = 20,910 yes/no questions with evidence) and `family_classification` (multiclass: 200 labeled contracts into the 25 families + `other`). Runs reuse the vendored `BaseAgent` machinery (retry contract, usage accounting, truncation) but add no dependencies and never touch the pipeline's own eval tasks. On completion a run is (1) scored deterministically (accuracy, macro per-category accuracy, yes-class F1, ECE calibration / strict + equiv family accuracy, macro-F1 — no LLM grading), (2) traced to Langfuse as one `legalbench-<task>` trace with per-question spans + run-level `legalbench_*` scores (configs in `observability/scores.py`), and (3) appended to the shared experiment log — regenerating the markdown log, the experiment-log site data, and the synced copy at `docs/reports/experiments/experiment_log.md`. The log/site regeneration runs the sibling repo's own scripts (`render_experiment_log.py`, `build_site.py`) via `LEGALBENCH_SIBLING_REPO` (default `../llm-entity-extraction`) and only when the log path lives inside that repo — a throwaway `--jsonl` never clobbers the real site data. Mock runs (`--mock`) use a deterministic fake model and are labelled `mock/mock-legalbench` in the log.

## Langfuse project configuration & tracing best practices

### Our Langfuse setup (verified Aug 2026)

- **Cloud org `Jack's Organization` → project `llm-mailroom`** on US cloud (`https://us.cloud.langfuse.com`). Credentials live in `.env` (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`). The project-scoped API keys cannot read org-level resources (`get_organization_*` → 403); org endpoints require org-scoped keys.
- **Environments**: every entrypoint declares `OBSERVABILITY_ENVIRONMENT` via `pipeline.env:default_environment()` — `live` (watcher, API, ops monitor), `pilot` (`scripts/run_pilot.py`), `misc` (sync/mirroring scripts), `mock` (when `OBSERVABILITY_PROVIDER=none`). The environment is **immutable per trace**: re-running a document reuses its deterministic trace id and keeps the first run's environment/tags (verified: the 12 pilot traces created before env wiring are stuck at `default`/`development`).
- **Trace structure** (`graph/build_graph.py:run_pipeline` → `pipeline_trace`): one trace per document named `document-pipeline`, deterministic trace id seeded from the filename (correlates with our DB/catalog), `session_id = matter_id` by default (groups all documents of a matter in the Sessions view) — pilot runs override it with a run-scoped `pilot-<mode>-<timestamp>` session plus `run_id` in `metadata` — curated input (file metadata, not raw payloads) / output (report), `metadata={pipeline, run_deadline, attempt, run_id?}`, `tags`, `environment`. Every node runs as a verb-first span (`classify-document`, `extract-fields`, …) via `traced_node`; all LLM calls are auto-traced `generation` observations with model + usage via `langfuse.openai` patching.
- **15 managed prompts** `mailroom-<agent_name>` (`production` label; current versions are verified by `scripts/sync_prompts.py`) — including `mailroom-image_extractor` and the judge variants `mailroom-judge-classification` / `mailroom-judge-correctness` (every LLM call links its exact prompt version); generations carry `langfuse_prompt=` so every trace links its prompt version.
- **Model registry** (synced from `taxonomy.yaml` `cost_models:` via `scripts/sync_models.py`): `qwen/qwen3.7-flash` ($0.03/$0.13 per 1M), `deepseek/deepseek-v4-flash` ($0.05/$0.25), `deepseek/deepseek-v4-pro` ($0.435/$0.87). Prices are verified against the live OpenRouter models API. Cost gotchas: (1) generation cost is computed **at ingestion time** and read from the observation **`cost_details`** field — `usage.input_cost`/`output_cost` are always null in API v2 responses; (2) the worker caches "model not found" per model string in Redis for **24h**, so a model used *before* its registry entry exists silently costs $0 until the cache is cleared — `sync_models.py --force` (delete + create) clears it.
- **One LLM connection**: OpenRouter (adapter `openai`, base `https://openrouter.ai/api/v1`, `custom_models=[deepseek/deepseek-v4-pro]`, `with_default_models=true`) — used by the LLM-as-a-Judge evaluators.
- **Score configs** auto-created idempotently by `ensure_score_configs()` (self-evident run scores: `parse_error`, `schema_valid`, `stage_completed`, `success_rate` (first-pass archive, no GT), `guardrail_triggered`, confidences, `estimated_cost_usd`, `total_tokens`, …; pilot ground truth: `class_correct`, `stage_correct`, `confidence_calibration_error`, `expected_field_presence`; judge dimensions: `classification_*`, `completeness`, `extraction_correctness`; deterministic field scoring: `extraction_field_score`, `extraction_overall_score`, `extraction_needs_judge_review`, `entity_list_precision`, `entity_list_recall`, `extraction_category_presence`; LegalBench suite: `legalbench_accuracy`, `legalbench_macro_f1`, `legalbench_calibration_error`, `legalbench_n_questions`, `legalbench_task`). `deterministic_verdict` is attached on grounded field-scoring traces but is **not** a `SCORE_CONFIGS` name (KANBAN-061 — it is not in the dojo 0.11.0 registry).
- **3 live datasets**: `mailroom-pilot` (13 original samples: 3 CUAD PDFs + 10 synthetic mock-only texts including three `insurance_claim` letters) plus per-corpus `mailroom-pilot-{atticus, legalbench}` (6 each). Pile of Law court opinions remain on disk but are not in the live manifest. Every item carries `expected_doc_class`, `expected_stage`, and schema-compatible literal `expected_fields` from `docs/examples/samples/manifest.csv`; `scripts/sync_dataset.py` rejects missing or unknown field truth.
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
- Adding a doc class touches ~5 places, all required: `taxonomy.yaml` (`doc_classes` + `agents:`), schema + `EXTRACTION_SCHEMAS` in `schemas/documents.py`, a `BaseAgent` subclass in `agents/`, a dispatch entry in `graph/build_graph.py:_build_specialist_dispatch` (the specialist-name→function map is hardcoded to 5 names), a prompt template entry in `llm/prompts.py:prompt_templates()`, and test fixtures/tests. `merger_agreement` is the exception: it is a live taxonomy class that reuses `contracts_specialist` / `ContractExtraction` (MAUD ≠ CUAD; no sixth specialist).
- Ollama runs as a profile-gated service in docker-compose: `--profile local-llm up`.

## Testing quirks

- No real LLM calls ever run in tests. `src/tests/conftest.py` patches `llm.client.OpenAI` and `agents.base.BaseAgent.__init__`. For new agent tests, inject `agent.client = <mock>` + `agent.model = "test-model"` like existing tests do.
- Tests run without Docker: conftest auto-sets `OPENROUTER_API_KEY` and `MAILROOM_BASE_DIR` to a tmpdir (`temp_base_dir` fixture). E2E tests build the full graph with mocked LLM and the SQLite checkpointer.
- `asyncio_mode = "auto"` is set; graph nodes are sync. Fixtures are plain-text files in `src/tests/fixtures/<doc_type>/`.

## Experiment-log sync (mirror of the extraction-pipeline repo)

The experiment log at `docs/reports/experiments/experiment_log.md` is a
**SYNCED MIRROR** of the `llm-entity-extraction` repo's
`reports/experiment_log.md` — never hand-edit it. It syncs naturally whenever
the upstream repo pushes new experiment runs or releases:

```bash
PYTHONPATH=src python -c "from legalbench.experiment_log import regenerate, default_log_path; regenerate(default_log_path())"
git add docs/reports/experiments/experiment_log.md
git commit -m "DOCS SYNC: experiment log re-synced"
git push origin main
```

`regenerate()` rebuilds the upstream markdown log + the GH Pages site data,
then refreshes this synced copy (SYNCED-DOCUMENT header carries the upstream
commit). **Direct access to the interactive site:** the synced log's header
links https://exios66.github.io/llm-entity-extraction/ — the filterable,
searchable experiment-log viewer (trend charts, cost-vs-quality scatter,
failure-mode bars, prompt diffs) served from the upstream repo's `docs/`
folder on `main`. Run the sync after every upstream push; the upstream repo's
own release workflow (`scripts/release.py`) also ends with this mirror step.

## Documentation layout

- `docs/` is the single source of truth for repository documentation (architecture, agents, configuration, deployment, testing, local models, reports). Do not duplicate its content anywhere else in the repo.
- **Reports convention (mandatory)**: every evaluation write-up, audit, or report goes under `docs/reports/<kind>/` — `audits/`, `pilots/`, `evaluations/` — created via `PYTHONPATH=src python src/scripts/new_report.py <kind> "TITLE"` (dated, kebab-case filename + standard header). Never put reports in the repo root or loose in `docs/`. `src/scripts/write_pilot_report.py` already defaults to `docs/reports/pilots/`.
- `docs/wiki/` holds **GitHub-wiki-only** pages (Home, Getting-Started, FAQ, _Sidebar, _Footer, README). It is NOT a mirror of `docs/` — never copy docs pages into it. `docs/wiki/sync-wiki.sh` pushes `docs/wiki/` to the GitHub wiki.
- `docs/agents.md` documents the pipeline's LLM agents — an architecture doc, not a coding-instruction file.
