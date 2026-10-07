# Running the pipeline

This page lists the commands you need to install `llm-mailroom`, start it, push documents through it, evaluate it, and check its audit trail. Every command comes from the code or the existing docs in this repo. For the full list of settings, see [Configuration](../pipeline-reference-llm-mailroom/configuration.md). For a guided first run across the whole constellation, see [Getting started](../start-here/getting-started.md).

All commands run from the repository root. Most scripts need `PYTHONPATH=src` because the code lives under `src/`.

## Prerequisites and install

| Need | Value | Source |
| --- | --- | --- |
| Python | 3.11 or newer (`requires-python = ">=3.11"`) | `pyproject.toml` |
| Package name / version | `mailroom` 0.8.0 | `pyproject.toml` |
| Database | None to install. SQLite file is created at `{MAILROOM_BASE_DIR}/mailroom.db` on first use | `.env.example` |
| Docker | Only for the optional Langfuse stack or container deploys | `README.md` |

### Standalone install with pip

```bash
git clone https://github.com/Exios66/llm-mailroom.git
cd llm-mailroom
cp .env.example .env              # add OPENROUTER_API_KEY for real runs
pip install -e ".[dev]"
```

The core dependencies include `llm-dojo-scoring`, pinned as a git dependency (`@v0.19.1`), so `pip` needs `git` and network access on first install.

### Optional extras

All extras are declared in `[project.optional-dependencies]` in `pyproject.toml`.

| Extra | Adds | Use it for |
| --- | --- | --- |
| `dev` | pytest, pytest-asyncio, pytest-mock, httpx, nbformat, nbclient, ipykernel | Running the test suite |
| `embeddings` | scipy, sentence-transformers | Embedding second signal and Hungarian matcher in field scoring (degrades gracefully without) |
| `postgres` | psycopg | Postgres storage engine (SQLite is the default) |
| `deploy` | modal 1.5.5 | Deploying the Modal vLLM app in `deploy/` (never imported by the pipeline) |
| `bert` | onnxruntime, tokenizers, huggingface-hub | BERT intake fast-path (ONNX inference only) |
| `notebooks` | ipywidgets, jupyterlab | `notebooks/dataset_browser.ipynb` |

```bash
pip install -e ".[dev,embeddings]"
```

### Install with uv (monorepo)

Inside the [Digital-Mailroom](https://github.com/LLM-Mailroom-Services/Digital-Mailroom) monorepo this repo is `packages/llm-mailroom`. Skip `pip install` there and run one `uv sync` at the monorepo root. `pyproject.toml` sets `[tool.uv.sources] llm-dojo-scoring = { workspace = true }`, so under uv the scoring engine comes from the workspace instead of the git pin. See [Other repos](#other-repos).

### Entry points

`pyproject.toml` declares **no** `[project.scripts]` console commands. Everything runs as `python -m <module>` or `python src/scripts/<name>.py`.

## Environment variables

Copy `.env.example` to `.env`. Scripts and the API load it automatically. The table lists the variables you need to start; [Configuration](../pipeline-reference-llm-mailroom/configuration.md) has the full reference.

| Variable | Required? | What it does |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Yes for any real LLM run on OpenRouter (the default provider) | OpenRouter key |
| `DEFAULT_PROVIDER` | No | Global provider override. `src/llm/providers.py` defines `openrouter` (default), `ollama`, `llamafile`, `vllm`, `litellm` and `generic`; the `.env.example` comment lists only `openrouter`, `ollama`, `vllm`, `generic` |
| `OLLAMA_BASE_URL`, `LLAMAFILE_BASE_URL`, `VLLM_BASE_URL`, `VLLM_API_KEY`, `GENERIC_BASE_URL`, `GENERIC_API_KEY` | Only for that provider | Local or self-hosted endpoints. See [Local models](../pipeline-reference-llm-mailroom/local-models.md) |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | Only with `DEFAULT_PROVIDER=litellm` | LiteLLM gateway endpoint (default `http://localhost:4000/v1`) and its master key. See [Full Docker stack](#full-docker-stack-mode-g) |
| `MAILROOM_LLM_FREE_ONLY` | No | When on, refuses to resolve any paid OpenRouter model |
| `MAILROOM_BASE_DIR` | No (default `./data`) | Where bins, `mailroom.db` and reports go |
| `OBSERVABILITY_PROVIDER` | No (default `auto`) | `langfuse`, `braintrust`, `phoenix`, `none`, or `auto` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Only for Langfuse tracing and the `sync_*` scripts | Langfuse project keys |
| `MAILROOM_API_HOST`, `MAILROOM_API_PORT`, `PORT` | No | API bind address and port (see [Start the API](#start-the-api-server)) |
| `MAILROOM_API_TOKEN` / `MAILROOM_API_TOKENS` | Required for any non-loopback bind | Bearer token(s) for the API |
| `MAILROOM_EMBED_WATCHER` | No (default on) | Set `0` when you run a separate watcher process |

### What happens with no API key

There is **no automatic fallback to a mock LLM**. Mock mode is always an explicit flag on a script (`--mock`).

- `resolve_provider()` in `src/llm/providers.py` raises `ValueError("OpenRouter API key not set (or is the mock placeholder)...")` whenever the provider is `openrouter` and `OPENROUTER_API_KEY` is empty or the literal placeholder `mock-key`. Every live entrypoint (watcher, API, ops monitor, `--real` runs) resolves its LLM client through this function.
- The API server itself has no key check at startup, so it starts, but each LLM call fails at client resolution.
- `run_pilot.py --real`, `run_hf_pilot.py --real` and `run_quality_judges.py --real` check the key up front and refuse to run. `gmail_smoke_test.py --llm real` exits with `--llm real requires OPENROUTER_API_KEY`.
- Self-hosted providers (`ollama`, `llamafile`, keyless `vllm`) need no key. Set `DEFAULT_PROVIDER` and the base URL instead. See [Local models](../pipeline-reference-llm-mailroom/local-models.md).

## Start the API server

The API is a FastAPI app in `src/api/main.py`. Running the module starts uvicorn with the bind host and port below, and also starts the inbox watcher in the same process (unless `MAILROOM_EMBED_WATCHER=0`).

```bash
PYTHONPATH=src python -m api.main
```

| Setting | Rule (from `listen_host()` / `listen_port()`) |
| --- | --- |
| Host | `MAILROOM_API_HOST`, default `127.0.0.1` |
| Port | `PORT` if set (Railway, Fly, Render), else `MAILROOM_API_PORT`, else `8000` |
| Off-loopback bind | Refused with `SystemExit` unless `MAILROOM_API_TOKEN` or `MAILROOM_API_TOKENS` is set (audit L-2) |

[API reference](../pipeline-reference-llm-mailroom/api.md) also documents a direct uvicorn start:

```bash
PYTHONPATH=src uvicorn api.main:app --host 0.0.0.0 --port 8000
```

This path skips the `assert_bind_allowed()` check in `__main__`, so set a token yourself when binding `0.0.0.0`.

Push a document through and follow it:

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@src/tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=MATTER-001"
curl http://localhost:8000/status/{doc_id}
curl http://localhost:8000/audit/{doc_id}
curl http://localhost:8000/health
```

Every route except `/api/relations/mode` is also mounted under `/v1` (`_mount_v1_aliases()`). Full endpoint list: [API reference](../pipeline-reference-llm-mailroom/api.md).

### Other long-running processes

```bash
# Dedicated watcher (only if the API runs with MAILROOM_EMBED_WATCHER=0)
PYTHONPATH=src python -m pipeline.watcher

# Ops monitor (system health sweeps)
PYTHONPATH=src python -m pipeline.ops_monitor

# Gmail intake, standalone debug (normally runs inside the watcher
# when MAILROOM_GMAIL_ENABLED=1)
PYTHONPATH=src python -m pipeline.gmail_intake

# Watchdog: polls the watcher heartbeat and emails a DOWN alert when the
# watcher dies or hangs (MAILROOM_WATCHDOG=0 disables it)
PYTHONPATH=src python -m pipeline.watchdog

# Optional local Langfuse stack (needs Docker)
docker compose -f src/config/docker/docker-compose.yml up -d postgres clickhouse langfuse-server
```

## Docker and Railway

This is a summary. Full steps, backups and troubleshooting are in the [Deployment guide](../pipeline-reference-llm-mailroom/deployment/README.md).

**Dockerfile.** Multi-stage build on `python:3.11-slim-bookworm`, runs as non-root user `mailroom` (uid 10001), exposes `7860`, and starts `python -m api.main`. The image sets `MAILROOM_API_HOST=0.0.0.0`, `MAILROOM_API_PORT=7860`, `MAILROOM_BASE_DIR=/data` and `MAILROOM_EMBED_WATCHER=1`, so it **needs `MAILROOM_API_TOKEN`** or it exits at start. Build arguments come from the Dockerfile header:

```bash
# Default (PIP_EXTRAS=bert + ModernBERT model bundle)
docker build -t mailroom:latest .

# Lean OpenRouter-only image
docker build --build-arg ML_BUILD_NONE=1 --build-arg PIP_EXTRAS= .
```

**Compose.** `deploy/docker-compose.yml` is the deployment-matrix base:

```bash
# Mode B (OpenRouter API)
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build

# Mode A (fully offline) with the Ollama overlay
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ollama.yml \
  --env-file .env up -d --build

# Producer for The-Mailroom Observatory
docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build
```

Mode M (one Modal-hosted vLLM server) is env-only: `modal deploy deploy/modal_vllm.py`, then `DEFAULT_PROVIDER=vllm`, `VLLM_BASE_URL` and `VLLM_API_KEY`. See [Modal + vLLM](../pipeline-reference-llm-mailroom/deployment/modal-vllm.md). Mode G (LiteLLM gateway plus Modal GPU tiers) has its own compose file: see [Full Docker stack](#full-docker-stack-mode-g).

**Railway.** `railway.json` uses the `DOCKERFILE` builder, health-checks `/health` (timeout 300 s), and restarts `ON_FAILURE` up to 10 times. `nixpacks.toml` is only a fallback (installs with `pip install .`, starts `PYTHONPATH=src python -m api.main`). Required service variables are `MAILROOM_API_TOKEN` and `OPENROUTER_API_KEY`; the API prefers Railway's `$PORT`.

```bash
railway link   # once
railway variables set MAILROOM_API_TOKEN=... OPENROUTER_API_KEY=...
railway up -m "mailroom producer"
```

## Full Docker stack (Mode G)

Mode G runs the whole pipeline on one host: the mailroom app talks to a LiteLLM gateway, which routes each agent to a Modal GPU tier or to OpenRouter. This section is a short run guide. The full reference (services, startup order, every `.env` variable, tier defaults) is on [Docker deployment, Mode G](../pipeline-reference-llm-mailroom/deployment/docker-deployment.md#mode-g--full-stack-litellm--modal-gpu-tiers), and the Modal side (per-tier knobs, single-tier Mode M) is on [Modal + vLLM](../pipeline-reference-llm-mailroom/deployment/modal-vllm.md).

Source files: [deploy/docker-compose.full.yml](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.full.yml), [deploy/litellm/config.yaml](https://github.com/Exios66/llm-mailroom/blob/main/deploy/litellm/config.yaml), [deploy/modal_vllm.py](https://github.com/Exios66/llm-mailroom/blob/main/deploy/modal_vllm.py), [src/scripts/smoke_modal_tiers.py](https://github.com/Exios66/llm-mailroom/blob/main/src/scripts/smoke_modal_tiers.py).

### 1. Deploy the Modal tiers

From the `deploy/modal_vllm.py` docstring:

```bash
pip install -e ".[deploy]" && modal token new      # once
export MODAL_VLLM_API_TOKEN="$(openssl rand -hex 24)"
modal run deploy/modal_vllm.py::download_model --tier fast   # optional pre-warm, per tier
modal deploy deploy/modal_vllm.py
```

The deploy prints one URL per tier (`fast`, `extract`, `vision`). `MODAL_VLLM_TIERS` picks which tiers deploy.

### 2. Fill `.env`

The compose file stops with an error unless these are set: `MAILROOM_API_TOKEN`, `LITELLM_MASTER_KEY`, `MODAL_FAST_URL`, `MODAL_EXTRACT_URL`, `MODAL_VISION_URL`. `POSTGRES_PASSWORD` is also required (the Postgres image will not start without it). Add `MODAL_VLLM_API_TOKEN` (the bearer you deployed with), `OPENROUTER_API_KEY` only if an agent is on the `api` tier, and `LANGFUSE_*` keys for Langfuse Cloud tracing. The commented block at the end of `.env.example` lists them all. The compose file sets `DEFAULT_PROVIDER=litellm` and `LITELLM_BASE_URL=http://llm-gateway:4000/v1` for every mailroom process.

### 3. Start the stack

```bash
docker compose -f deploy/docker-compose.full.yml --env-file .env up -d --build

# With the local Phoenix tracing sink (UI on 127.0.0.1:6006)
docker compose -f deploy/docker-compose.full.yml --env-file .env --profile phoenix up -d --build
```

The gateway image defaults to `ghcr.io/berriai/litellm:v1.104.0` (override with `LITELLM_IMAGE`; `.env.example` says not to use `main-stable`). The API is published on `${MAILROOM_API_BIND:-127.0.0.1}:${MAILROOM_API_PORT:-8000}`. Because a token is set, send `Authorization: Bearer <MAILROOM_API_TOKEN>` with the `curl` calls from [Start the API server](#start-the-api-server).

### 4. Move an agent between tiers

Each agent's tier is `agents.<name>.tier` in `src/config/taxonomy.yaml` (`gateway.default_tier: fast`; tiers `fast`, `extract`, `vision`, `api`). Override it at runtime without editing the file:

```bash
MAILROOM_GATEWAY_TIERS="sorter=api,boss=extract"
```

The `api` tier has no alias: the agent sends its OpenRouter model slug and the gateway forwards it to OpenRouter.

### 5. Smoke test the tiers

`smoke_modal_tiers.py` exits 0 only when every check passes and writes a JSON and markdown report under `<MAILROOM_BASE_DIR>/smoke_runs/<stamp>/`.

```bash
# Network-free routing contract: taxonomy tiers, LiteLLM aliases and Modal tiers agree
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --check

# Wake every GPU tier in parallel (one 1-token completion each)
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --warm

# One document per doc class through the deployed API, then check each
# node's Langfuse generation model against its tier
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --run --api-url http://127.0.0.1:8000

# Only audit an existing Langfuse session
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --verify-session <SESSION_ID>
```

Other flags: `--in-process` (call `run_pipeline()` locally instead of the API), `--source hf|snapshot` (default `hf`), `--classes`, `--target-chars` (default 8000), `--timeout` (default 3600 s), `--skip-langfuse`. `--api-url` defaults to `MAILROOM_SMOKE_API_URL` or `http://127.0.0.1:8000`.

## Pipeline scripts

All scripts live in `src/scripts/` and run from the repo root. Most real/mock scripts make the mode explicit: `run_pilot.py` and `run_quality_judges.py` refuse to run without `--mock` or `--real`.

{% hint style="info" %}
The committed pilot sample set (`docs/examples/samples/manifest.csv` and its PDFs) is **not tracked in this checkout** (the tests call it a "pruned heavy asset; see upstream repo"). Scripts that read it (`run_pilot.py`, `prepare_samples.py`, `run_quality_judges.py`, `run_vision_sweep.py`, `validate_pipeline.py --sources/--pdfs`) fail with `FileNotFoundError` until you restore it. `validate_pipeline.py --fixtures` works on the in-repo fixtures under `src/tests/fixtures/`.
{% endhint %}

### Run documents through the pipeline

| Script | What it does | Main flags |
| --- | --- | --- |
| `validate_pipeline.py` | Runs committed documents through the full pipeline in mock mode and prints per-document routing plus per-class and per-subtype accuracy | `--fixtures`, `--sources`, `--pdfs`, `--all` (default), `--report PATH` |
| `run_pilot.py` | Runs the pilot sample set through the real LangGraph pipeline and scores against the manifest. Writes `{MAILROOM_BASE_DIR}/pilot_report_<run_tag>.json` (plus a dated baseline copy on `--real`) | `--mock` or `--real` (one is required), `--include CLASS`, `--source CORPUS`, `--max-docs N`, `--baseline REPORT`, `--scores/--no-scores` |
| `run_hf_pilot.py` | Hugging Face corpus pilot (default `Lucius-Morningstar/mailroom-dataset`). Writes `data/hf_pilot/<stamp>/report.json` | One of `--check`, `--mock`, `--real`, `--finalize REPORT`; `--per-class N` (default 1), `--per-subclass N`, `--dataset SLUG`, `--examples`, `--resume REPORT`, `--shared-matter`, `--split`, `--max-chars`, `--target-chars`, `--max-scan` |
| `prepare_samples.py` | Builds the pilot PDF set in `data/samples/` from the manifest | none |

```bash
PYTHONPATH=src python src/scripts/validate_pipeline.py --fixtures
PYTHONPATH=src python src/scripts/run_pilot.py --mock
PYTHONPATH=src python src/scripts/run_pilot.py --real --scores
PYTHONPATH=src python src/scripts/run_pilot.py --mock --baseline data/pilot_report.json
PYTHONPATH=src python src/scripts/run_hf_pilot.py --check
PYTHONPATH=src python src/scripts/run_hf_pilot.py --mock --per-class 1
PYTHONPATH=src python src/scripts/run_hf_pilot.py --real --per-class 1
```

`--real` runs only on real samples (CUAD/Atticus and LegalBench); synthetic samples are mock-only and are refused by `--real`.

### Evaluate

| Script | What it does | Main flags |
| --- | --- | --- |
| `run_quality_judges.py` | LLM-as-a-judge over a pilot report: classification, completeness, correctness | `--mock` or `--real`, `--judges LIST`, `--report PATH` (default `{MAILROOM_BASE_DIR}/pilot_report.json`), `--hf-report PATH` (repeatable), `--hf-latest N`, `--on-existing-traces`, `--new-judge-traces` |
| `run_agent_eval.py` | Evaluates one agent in isolation (no 13-node graph) | `--list`, `--agent NAME\|all`, `--mock`, `--real`, `--n N`, `--self-check`, `--json` |
| `run_vision_sweep.py` | Runs the same documents as text-only, vision-10 and vision-all (each a separate `run_pilot.py` process) | `--real` or `--mock`, `--include`, `--source`, `--max-docs N` (default 3), `--configs`, `--dry-run` |
| `write_pilot_report.py` | Renders the vision-tradeoff pilot report from `data/vision_sweep/*.json` | `--out PATH`, `--dry-run` |
| `calibrate_field_scoring.py` | Reports per-field-type score separation and calibrated `field_scoring.type_bands` cutoffs | `--band LOW HIGH`, `--json`, `--seed`, `--max-fields` |

`run_pilot.py` writes a run-scoped `pilot_report_<run_tag>.json`, while `run_quality_judges.py` reads `pilot_report.json` by default, so pass `--report` with the file the pilot printed.

```bash
PYTHONPATH=src python src/scripts/run_quality_judges.py --mock --report data/pilot_report_<run_tag>.json
PYTHONPATH=src python src/scripts/run_quality_judges.py --real --hf-latest 1
PYTHONPATH=src python src/scripts/run_agent_eval.py --list
PYTHONPATH=src python src/scripts/run_agent_eval.py --agent all --mock --n 1 --self-check
PYTHONPATH=src python src/scripts/run_agent_eval.py --agent sorter --real --n 5
PYTHONPATH=src python src/scripts/run_vision_sweep.py --real --max-docs 3
```

### LegalBench suite

A separate evaluation harness in `src/legalbench/` (not part of the installed package, so keep `PYTHONPATH=src`). Tasks: `contract_qa` and `family_classification`. Both read the full CUAD corpus under `data/cuad/`, which `fetch_full_cuad.py` downloads.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--task` | `contract_qa` | `contract_qa` or `family_classification` |
| `--n` | 30 | Questions or documents to sample |
| `--seed` | 42 | Sample seed |
| `--model` | `DEFAULT_MODEL` in `legalbench/runner.py` | OpenRouter model id |
| `--mock` | off | Fake model, no key, not real results |
| `--no-trace` / `--no-log` | off | Skip Langfuse / skip the experiment log |
| `--jsonl PATH` | | Experiment-log path override |
| `--list-tasks` | | List tasks and exit |

```bash
PYTHONPATH=src python src/scripts/fetch_full_cuad.py
PYTHONPATH=src python -m legalbench.cli --list-tasks
PYTHONPATH=src python -m legalbench.cli --task family_classification --n 20 --mock
PYTHONPATH=src python -m legalbench.cli --task contract_qa --n 30 --model qwen/qwen3.7-flash
```

### Inspect and audit

| Script | What it does | Main flags |
| --- | --- | --- |
| `compare_runs.py` | Tabulates per-run metrics from the catalog (SQLite or `DATABASE_URL`): duration, tokens, cost, LLM calls, attempts, confidences, abort status | `--limit N`, `--doc-type TYPE`, `--json` |
| `verify_audit_chains.py` | Recomputes every per-document SHA-256 audit chain; nonzero exit on any break | `--doc DOC_ID`, `--json` |
| `analyze_audit_db.py` | Summarizes the audit DB: event and actor histograms, chain health, review events | `--json`, `--no-verify`, `--recent N` (default 20), `--join-catalog` |
| `recover_processing.py` | Finds documents stranded in `processing/` by a crashed worker; re-queues or retires them. Dry run by default | `--apply`, `--stale-minutes N` (default 60), `--move-all-to-failed`, `--catalog` |
| `export_warehouse.py` | Exports archived/failed catalog rows and audit chains to daily Parquet under `data/warehouse/` | `--full`, `--date`, `--since`, `--doc-id`, `--json`, `--show-manifest` |
| `gmail_smoke_test.py` | Gmail plus watcher connectivity smoke using an insurance-claim fixture. Default is network-free with a fake IMAP client and mock LLM | `--real` (sends and drains the real mailbox), `--llm mock\|real`, `--matter`, `--fixture` |
| `probe_hosted_spaces.py` | Probes the live Hugging Face Observatory and producer Spaces | `--offline`, `--json` |

```bash
PYTHONPATH=src python src/scripts/compare_runs.py --limit 50
PYTHONPATH=src python src/scripts/verify_audit_chains.py
PYTHONPATH=src python src/scripts/analyze_audit_db.py --join-catalog
PYTHONPATH=src python src/scripts/recover_processing.py            # dry run
PYTHONPATH=src python src/scripts/recover_processing.py --apply
PYTHONPATH=src python src/scripts/gmail_smoke_test.py              # network-free
PYTHONPATH=src python src/scripts/gmail_smoke_test.py --real --llm real
```

`gmail_smoke_test.py --real` marks every unseen message in the mailbox as seen. Run it on a quiet mailbox. See [Gmail intake](../pipeline-reference-llm-mailroom/gmail-intake.md).

### Sync and maintenance

The `sync_*` scripts need `LANGFUSE_*` keys and are idempotent.

| Script | What it does | Main flags |
| --- | --- | --- |
| `sync_prompts.py` | Pushes agent prompt templates to Langfuse Prompt Management | `--dry-run`, `--force`, `--agent NAME` |
| `sync_evaluators.py` | Creates or updates the two LLM-as-a-judge evaluators and their rules (needs `OPENROUTER_API_KEY`) | `--dry-run`, `--force`, `--disable`, `--provider`, `--model` |
| `sync_dataset.py` | Mirrors pilot samples into Langfuse datasets | `--dry-run`, `--limit N`, `--include CLASS`, `--dataset SOURCE` |
| `sync_models.py` | Syncs `cost_models` prices to the Langfuse model registry | `--dry-run`, `--force`, `--model` |
| `sync_dashboards.py` | Syncs the two health dashboards to Langfuse | `--dry-run` |
| `sync_langfuse_logs.py` | Pulls traces into `data/langfuse_logs/` for offline analysis | `--since` (default 24h), `--limit` (default 100), `--trace-id`, `--output`, `--wait-scores` |
| `sync_hf_ground_truth.py` | Derives and publishes intent/subject/keyword ground truth for the HF dataset (`--push` needs `HF_TOKEN`) | `--check`, `--mock`, `--real`, `--dry-run`, `--reuse`, `--resume`, `--push`, `--limit`, `--model`, `--out` |
| `fetch_external_samples.py` | Downloads LegalBench MAUD, CUAD and Pile of Law samples | `--source`, `--force` |
| `fetch_full_cuad.py` | Downloads full CUAD to `data/cuad/` and writes the EDA | `--data-dir`, `--skip-download`, `--no-eda`, `--force` |
| `cutover.py` | Per-agent provider/model switching in `taxonomy.yaml`. See [Local models](../pipeline-reference-llm-mailroom/local-models.md) | `--list`, `--list-models`, `--recommend`, `--agent`, `--all`, `--provider`, `--model`, `--validate` |
| `bump_dojo_scoring.py` | Checks or bumps the `llm-dojo-scoring` git pin | `--check`, `--apply`, `--tag`, `--dry-run`, `--allow-missing-release` |
| `publish_space.py` | Publishes the producer Docker Space (`--check` makes no Hub calls) | `--check`, `--repo`, `--private`, `--secrets-only`, `--skip-secrets`, `--message` |
| `new_report.py` | Scaffolds a dated report file under the reports tree | positional `kind` and `title`, `--date`, `--dry-run` |

```bash
PYTHONPATH=src python src/scripts/sync_prompts.py --dry-run
PYTHONPATH=src python src/scripts/sync_langfuse_logs.py --since 24h
PYTHONPATH=src python src/scripts/cutover.py --list
PYTHONPATH=src python src/scripts/publish_space.py --check
```

## Run the tests

```bash
pytest src/tests/ -v
pytest src/tests/test_pipeline_e2e.py -v
pytest src/tests/ --cov=src --cov-report=html
```

`pyproject.toml` sets `testpaths = ["src/tests"]` and `pythonpath = ["src", "."]`, so plain `pytest` works from the repo root. Tests never call a real LLM (`src/tests/conftest.py` mocks the client). Details: [Testing guide](../pipeline-reference-llm-mailroom/testing.md).

## Other repos

### local-mailroom-sandbox

Offline sandbox with its own `sandbox` CLI. From its [README](https://github.com/Exios66/local-mailroom-sandbox/blob/main/README.md):

```bash
pip install -e ".[dev]"
cp config/.env.example .env
sandbox up                 # Langfuse + Ollama
sandbox pull-models        # ollama pull qwen3:8b
sandbox pilot --mock       # machinery only, no LLM
sandbox pilot --local      # real local model
```

### Digital-Mailroom monorepo

One uv workspace holding every constellation repo. From its [README](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/README.md):

```bash
uv sync                                          # install every package editable into .venv
uv run pytest packages/llm-mailroom/src/tests    # this repo's tests
python scripts/sync_packages.py status           # subtree sync status
```

More in [Getting started](../start-here/getting-started.md).

## See also

[Pipeline flowchart](flowchart.md) for every graph edge, [Extraction schemas](extraction-schemas.md) for the fields each specialist returns, and [Scoring and performance](scoring-and-metrics.md) for how runs are scored.

## Sources

- [pyproject.toml](https://github.com/Exios66/llm-mailroom/blob/main/pyproject.toml), [.env.example](https://github.com/Exios66/llm-mailroom/blob/main/.env.example), [README.md](https://github.com/Exios66/llm-mailroom/blob/main/README.md)
- [src/api/main.py](https://github.com/Exios66/llm-mailroom/blob/main/src/api/main.py), [src/llm/providers.py](https://github.com/Exios66/llm-mailroom/blob/main/src/llm/providers.py), [src/llm/client.py](https://github.com/Exios66/llm-mailroom/blob/main/src/llm/client.py)
- [Dockerfile](https://github.com/Exios66/llm-mailroom/blob/main/Dockerfile), [railway.json](https://github.com/Exios66/llm-mailroom/blob/main/railway.json), [nixpacks.toml](https://github.com/Exios66/llm-mailroom/blob/main/nixpacks.toml), [deploy/docker-compose.yml](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.yml), [deploy/README.md](https://github.com/Exios66/llm-mailroom/blob/main/deploy/README.md)
- [src/scripts/](https://github.com/Exios66/llm-mailroom/blob/main/src/scripts/README.md) (argparse in each script), [src/legalbench/cli.py](https://github.com/Exios66/llm-mailroom/blob/main/src/legalbench/cli.py), [src/legalbench/README.md](https://github.com/Exios66/llm-mailroom/blob/main/src/legalbench/README.md)
- [deploy/docker-compose.full.yml](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.full.yml), [deploy/litellm/config.yaml](https://github.com/Exios66/llm-mailroom/blob/main/deploy/litellm/config.yaml), [deploy/modal_vllm.py](https://github.com/Exios66/llm-mailroom/blob/main/deploy/modal_vllm.py), [src/scripts/smoke_modal_tiers.py](https://github.com/Exios66/llm-mailroom/blob/main/src/scripts/smoke_modal_tiers.py), [src/config/taxonomy.yaml](https://github.com/Exios66/llm-mailroom/blob/main/src/config/taxonomy.yaml)
- [local-mailroom-sandbox README](https://github.com/Exios66/local-mailroom-sandbox/blob/main/README.md), [Digital-Mailroom README](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/README.md)
