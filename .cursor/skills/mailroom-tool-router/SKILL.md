---
name: mailroom-tool-router
description: Choose the correct llm-mailroom tool for serving, tracing, Hub data, scoring, and deploy. Use at the start of any provider, observability, Hugging Face, Ollama, Modal, Langfuse, Braintrust, Phoenix, LangGraph, dojo, or LegalBench task so only the most appropriate skill/stack is used.
---

# Mailroom tool router

Read this skill **first** when the task touches providers, tracing, datasets,
Docker, scoring, or deploys. Then open exactly one specialty skill below. Do
not invent a second tracing backend or cloud LLM path when the repo default
already fits.

## Decision table

| Job | Use | Skill | Do **not** use |
| --- | --- | --- | --- |
| Default production LLM | OpenRouter via `get_llm(agent_name)` | [openrouter](../openrouter/SKILL.md) | Hardcoding a model in agent code; Ollama unless local was requested |
| Local / offline inference | Ollama (`DEFAULT_PROVIDER=ollama`) | [ollama](../ollama/SKILL.md) | OpenRouter ids against Ollama |
| Remote GPU OpenAI `/v1` | Modal `mailroom-vllm` | [modal](../modal/SKILL.md) | The sandbox app name `sandbox-vllm` |
| Reachable REVIEW producer | Docker / HF Space (`MAILROOM_PIPELINE_URL`) | [huggingface](../huggingface/SKILL.md) (publisher) + [langfuse](../langfuse/SKILL.md) (visualizer knobs) | Pointing `MAILROOM_API_URL` at `:8000` |
| Default tracing + The-Mailroom | Langfuse 4.x (`document-pipeline`) | [langfuse](../langfuse/SKILL.md) | Phoenix as the visualizer source |
| Local cost-free OTEL UI | Arize Phoenix (`phoenix serve`) | [apache-phoenix](../apache-phoenix/SKILL.md) | Claiming Phoenix feeds The-Mailroom |
| Hosted experiment / auto-improve loop | Braintrust | [braintrust](../braintrust/SKILL.md) | Braintrust as the default live sink |
| Hub class/subtype corpora | `run_hf_pilot.py` + `hf_corpora.py` | [huggingface](../huggingface/SKILL.md) | Invented stand-in class texts; loading all 247k Enron rows by default |
| Graph nodes, routing, HITL | LangGraph in `graph/` | [langgraph](../langgraph/SKILL.md) | A second orchestrator |
| Extraction / class KPIs | `llm-dojo-scoring @v0.12.1` | [dojo-scoring](../dojo-scoring/SKILL.md) | Inventing `SCORE_CONFIG` names not in the registry |
| LegalBench QA / family class | `python -m legalbench.cli` | [legalbench](../legalbench/SKILL.md) | Treating `legalbench-full` as pipeline ingest |

## Observability precedence (`OBSERVABILITY_PROVIDER`)

Canonical `auto` chain (`observability/tracing.py`):

1. Langfuse if `LANGFUSE_SECRET_KEY` is set
2. else Braintrust if `BRAINTRUST_API_KEY` is set
3. else Phoenix if `PHOENIX_TRACING` ≠ `disabled`
4. else `none`

Tests force `none`. The-Mailroom reads **Langfuse only** for envelopes.

## Serving precedence

1. **`--mock` / pytest** — fake LLM, no network
2. **OpenRouter** — production default (`OPENROUTER_API_KEY` required or `get_llm` raises)
3. **Ollama** — local cutover (`DEFAULT_PROVIDER=ollama`)
4. **vLLM local** — `DEFAULT_PROVIDER=vllm` + `VLLM_BASE_URL`
5. **Modal vLLM** — remote GPU; `pip install -e ".[deploy]"` + `modal deploy`
6. **generic** — any other OpenAI-compatible base URL

No agent code names a provider/model. `taxonomy.yaml` `agents:` + `get_llm(agent_name)` only. `DEFAULT_PROVIDER` overrides globally.

## Repo anchors

| Concern | Path |
| --- | --- |
| Taxonomy / models | `src/config/taxonomy.yaml` |
| Env template | `.env.example` |
| Providers | `src/llm/providers.py`, `docs/configuration.md` |
| Compose | `src/config/docker/docker-compose.yml` |
| REVIEW producer image | root `Dockerfile`, `deploy/docker-compose.producer.yml`, `src/scripts/publish_space.py` |
| Modal app | `deploy/modal_vllm.py` |
| Tracing | `src/observability/tracing.py` |
| Hub corpora | `src/pipeline/hf_corpora.py` |
| Cursor skills (this tree) | `.cursor/skills/*/SKILL.md` |
| Vendored depth | `.opencode/skills/` |
