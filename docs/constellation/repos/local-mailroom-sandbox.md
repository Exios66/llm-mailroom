# local-mailroom-sandbox

**Run the full pipeline offline on local models, and run GPU serving studies on Modal.**

| | |
| :--- | :--- |
| Repository | [Exios66/local-mailroom-sandbox](https://github.com/Exios66/local-mailroom-sandbox) |
| Monorepo path | `packages/local-mailroom-sandbox` (Python package `mailroom-sandbox`) |
| Default model | Ollama `qwen3:8b` |
| Board | `governance/TASKS.md` (`SAND-*` cards) |

## What it does

The sandbox runs the real classify, extract, report and archive graph without any API provider: on Ollama, vLLM, llama.cpp or LM Studio. OpenRouter can be swapped in when you want an API comparison.

It does not fork the pipeline. Tracked snapshots of llm-mailroom and llm-dojo-scoring live under `vendor/`, so a fresh clone works with no network access. Config, serving, eval and scoring overlays sit on top.

It is also where the constellation's local-versus-API and GPU serving studies happen: the L4 and A100 runbooks, the specialist grid, Modal vLLM serving, and the `sandbox watch` live log view.

## Quick start

```bash
pip install -e ".[dev]"
cp config/.env.example .env
sandbox up              # Langfuse + Ollama compose profiles
sandbox pull-models     # ollama pull qwen3:8b
sandbox health
sandbox pilot --mock    # machinery only, no LLM
sandbox pilot --local   # real local model
```

`sandbox fetch-deps` refreshes the vendored snapshots (from the workspace in the monorepo, or from release tags in a standalone clone).

## The CLI at a glance

| Command | What it does |
| :--- | :--- |
| `sandbox up / down / health / profiles` | Manage the local services and provider profiles |
| `sandbox pilot`, `sandbox hf-pilot`, `sandbox legalbench` | Pilots over fixtures, the Hub corpus, or LegalBench |
| `sandbox eval <agent> / extract / chained / pipeline / local_vs_api` | Evaluations, including local versus API-key comparisons |
| `sandbox matrix ...` | Provider by model by prompt matrices |
| `sandbox datasets pull / sample / prepare` | Pinned full Hub pull, offline per-class samples, cleaning |
| `sandbox run preflight / start / status / resume / cancel` | Long runs from a run spec in `config/runs/`, locally or as a Modal job |
| `sandbox watch [--web]` | Mailroom-themed live view of a run |
| `sandbox cutover --profile <p> --model <m>` | Point agents at a local model |

A vLLM or Modal profile without `--local` warns instead of silently falling back to mock.

## Layout

| Path | Contents |
| :--- | :--- |
| `config/profiles/`, `config/runs/`, `config/prompts/` | Provider profiles, run specs, frozen prompt stems |
| `config/models.yaml` | Map from OpenRouter champions to local model tags |
| `deploy/` | Dockerfile, compose, Modal vLLM and job worker, HTCondor, conda |
| `vendor/` | Tracked snapshots of llm-mailroom and llm-dojo-scoring |
| `api-evals/` | A separate OpenRouter cost harness with its own CLI and run specs |
| `reports/` | The sandbox's own experiment log and serving reports |

The layout contract, including which paths are frozen by tests, is [docs/setting-up/LAYOUT.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/LAYOUT.md).

## Its documentation

- [docs/setting-up/QUICKSTART.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/QUICKSTART.md) — start here: install, full CLI reference, workflows
- [docs/setting-up/providers.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/providers.md) — Ollama, vLLM, Modal, llama.cpp, LM Studio, OpenRouter
- [docs/setting-up/remote-serving.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/remote-serving.md) and [docker-offline.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/docker-offline.md)
- [docs/evals.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/evals.md) — runners, matrix, scoring, experiment log
- [docs/tracing.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/tracing.md) — Langfuse v4 data model and tags
- [docs/jobs.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/jobs.md) — long-running jobs
- [docs/SANDBOX-GUIDE.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/SANDBOX-GUIDE.md)
- [docs/BENCHMARK-PROGRAM.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/BENCHMARK-PROGRAM.md), [docs/scale-matrix.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/scale-matrix.md), [docs/SPECIALIST-GRID-PLAN.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/SPECIALIST-GRID-PLAN.md), [docs/RUN-COST-DERIVATION.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/RUN-COST-DERIVATION.md)
- [docs/modal/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/modal) — Modal serving operations, document jobs, L4 benchmark
- [docs/runbooks/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/runbooks) — operator runbooks per specialist and GPU
- [docs/pretty-logging/mailroom-themed-logging.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/pretty-logging/mailroom-themed-logging.md) — `sandbox watch`
- [docs/datasets/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/datasets) — dataset card
