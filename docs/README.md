# `docs/` — Written documentation

## What this folder is (plain English)

This is the manual for the project — deeper, more detailed versions of everything summarized in the READMEs. Start with the root `README.md` to understand the shape of the system, then dive into whichever doc matches your question.

## The files

| File | What it covers |
|---|---|
| `architecture.md` | The full system design: the 11-node state machine, data flow, audit trail |
| `agents.md` | Every LLM agent: its role, personality, and output schema |
| `configuration.md` | `config/taxonomy.yaml` field-by-field + all environment variables |
| `api.md` | Every HTTP endpoint with example requests/responses |
| `deployment.md` | Running the system in production |
| `testing.md` | How tests are organized, fixtures, and how to write new ones |
| `local-models.md` | Switching agents from cloud (OpenRouter) to local (Ollama) models |

## Technical reference

- `docs/` and `wiki/` are **mirrors**: `docs/agents.md` == `wiki/Agents.md`, and so on. `wiki/sync-wiki.sh` pushes `wiki/` to the GitHub wiki. When you edit a page in `docs/`, update the matching page in `wiki/` too (or run the sync script after).
- `docs/agents.md` is an architecture doc about the pipeline's LLM agents — it is NOT an instruction file for coding assistants (that's `AGENTS.md` at the repo root).
