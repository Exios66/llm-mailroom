<div align="center">

# 📚 Mailroom — Documentation

**The canonical documentation hub for the llm-mailroom pipeline — architecture, agents, configuration, API, deployment, and more.**

[![Docs](https://img.shields.io/badge/docs-architecture--agents--config--api-blue)](architecture.md)
[![Wiki](https://img.shields.io/badge/wiki-GitHub--wiki-purple)](wiki/)

</div>

---

## What This Folder Is

This is the manual for the project — deeper, more detailed versions of everything summarized in the READMEs. Start with the root [`README.md`](../README.md) to understand the shape of the system, then dive into whichever doc matches your question.

## Documentation Index

<div align="center">

| Guide | What It Covers |
|:---|:---|
| [`architecture.md`](architecture.md) | The full system design: the 13-node state machine, data flow, audit trail |
| [`agents.md`](agents.md) | Every LLM agent: its role, personality, and output schema |
| [`configuration.md`](configuration.md) | `config/taxonomy.yaml` field-by-field + all environment variables |
| [`api.md`](api.md) | Every HTTP endpoint with example requests/responses |
| [`deployment.md`](deployment.md) | Running the system in production |
| [`testing.md`](testing.md) | How tests are organized, fixtures, and how to write new ones |
| [`local-models.md`](local-models.md) | Switching agents from cloud (OpenRouter) to local (Ollama) models |
| [`sister-repos.md`](sister-repos.md) | The llm-mailroom umbrella: all governed sibling repos |
| [`SCORING.md`](SCORING.md) | Scoring methodology (field-type-aware, factuality audit, diagnostics) |

</div>

## Related Documentation

| Location | What |
|:---|:---|
| [`docs/reports/`](reports/) | Evaluation write-ups: audits, pilots, evaluations |
| [`docs/examples/`](examples/) | Sample documents + manifest ground truth |
| [`docs/wiki/`](wiki/) | GitHub-wiki-only pages (Home, Getting-Started, FAQ) |
| [`docs/slides/`](slides/) | Scoring-method decks with worked examples |
| [`docs/memos/`](memos/) | Archived research memoranda |

## Technical Notes

- `docs/` is the **single source of truth** for repository documentation. `docs/wiki/` contains only GitHub-wiki-native pages and is pushed via `docs/wiki/sync-wiki.sh` — it is **not** a mirror of `docs/`.
- `docs/agents.md` is an architecture doc about the pipeline's LLM agents — it is NOT an instruction file for coding assistants (that's `AGENTS.md` at the repo root).
- This repo is also `packages/llm-mailroom` in the [mailroom-dev](https://github.com/Exios66/mailroom-dev) monorepo. `docs/sister-repos.md` covers the subtree sync contract.

---

<div align="center">

**[llm-mailroom](https://github.com/Exios66/llm-mailroom)** ·
**[llm-entity-extraction](https://github.com/Exios66/llm-entity-extraction)** ·
**[llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring)**

<sub>Built by the governed evaluation family under <a href="https://github.com/Exios66">@Exios66</a> · 2026</sub>

</div>
