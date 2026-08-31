# `docs/` — Written documentation

## What this folder is (plain English)

This is the manual for the project — deeper, more detailed versions of everything summarized in the READMEs. Start with the root `README.md` to understand the shape of the system, then dive into whichever doc matches your question.

## The files

| File | What it covers |
|---|---|
| `architecture.md` | The full system design: the 13-node state machine, data flow, audit trail |
| `agents.md` | Every LLM agent: its role, personality, and output schema |
| `configuration.md` | `config/taxonomy.yaml` field-by-field + all environment variables |
| `api.md` | Every HTTP endpoint with example requests/responses |
| `deployment.md` | Running the system in production |
| `testing.md` | How tests are organized, fixtures, and how to write new ones |
| `local-models.md` | Switching agents from cloud (OpenRouter) to local (Ollama) models |

## Technical reference

- `docs/` is the **single source of truth** for repository documentation. `docs/wiki/` contains only GitHub-wiki-native pages (Home, Getting-Started, FAQ, _Sidebar, _Footer) and is pushed to the GitHub wiki via `docs/wiki/sync-wiki.sh` — it is **not** a mirror of `docs/`.
- `docs/agents.md` is an architecture doc about the pipeline's LLM agents — it is NOT an instruction file for coding assistants (that's `AGENTS.md` at the repo root).
- This repo is also `packages/llm-mailroom` in the [mailroom-dev](https://github.com/Exios66/mailroom-dev) monorepo (uv workspace + git subtree). `docs/sister-repos.md` § mailroom-dev covers the subtree sync contract and the monorepo task board; cross-repo work is claimed there before editing (cards `HUB-00N`).
