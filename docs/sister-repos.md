# Sister Repositories & the Mailroom Umbrella

llm-mailroom does not fly alone. It is the pipeline at the center of a small
constellation of governed repositories — each with its own repo, board
discipline, and release train — plus derived artifacts hosted elsewhere. Since
2026-08-30 the whole constellation also lives as **one monorepo**
([`Digital-Mailroom`](https://github.com/LLM-Mailroom-Services/Digital-Mailroom)): every family
repo is a git-subtree package under `packages/`, wired as a single `uv`
workspace, with the monorepo as the **source of truth for active
development**. This page maps who's who, what flows between them, and where
the canonical state of each lives. (Links verified 2026-08-23; monorepo
verified 2026-08-30.)

```
                        ┌──────────────────────────────┐
                        │          Digital-Mailroom        │
                        │  THE MONOREPO — uv workspace │
                        │  git-subtree packages + hub  │
                        │  task board (TASKS.md)       │
                        └──────────┬───────────────────┘
            sync_packages.py      │ subtree pull / push
            (status/pull/push)    ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│  llm-entity-extraction │   │        llm-mailroom          │
│  prompt-experiment loop│   │  (this repo — the pipeline)  │
└──────────┬─────────────┘   └──────────┬───────────────────┘
           │            ┌───────────────┴───────────────┐
           ▼            ▼                               ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│  llm-dojo-scoring      │◀──│        The-Mailroom          │
│  scoring engine        │   │   pixel-art visual engine    │
└────────────────────────┘   └──────────┬───────────────────┘
                 │                      ▼
                 │        (reads this repo's Langfuse project — US cloud:
                 │         every envelope, badge, verdict, metric on screen)
┌────────────────────────┐
│ agent-mailroom |       │  sibling stand-alone mailrooms / sandboxes live
│ local-mailroom-sandbox │  in the monorepo too (same law, independent train)
└────────────────────────┘

corpus feeds:   Enron-Evaluation-Environment, claims-data-eda   (virtual members)
derived site:   llm-mailroom-graph (graphify knowledge-graph site)
HF datasets:    Lucius-Morningstar/* (published eval/corpus surfaces)
```

## At a glance

| Repository | Role | Relationship to mailroom |
|---|---|---|
| [llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring) | Deterministic, field-type-aware scoring engine (metric registry, dedicated specialist suites, sorter subclass catalogs, computable intake clerk, prompt catalog, `local_vs_api` serving comparison) | **Upstream governed dependency**, pinned in `pyproject.toml` (`@v0.16.0` / [PR #11](https://github.com/Exios66/llm-dojo-scoring/pull/11)); auto-bump via `.github/workflows/bump-dojo-scoring.yml` |

## llm-dojo-scoring — the upstream scoring engine

- Pinned as a git dependency (`@v0.16.0` at time of writing); mailroom wires
  its `taxonomy.yaml` scoring block onto package Settings via
  `observability/field_scoring.py` (a deprecation shim — imports should move
  to `llm_dojo_scoring.field_scoring`).

## Governance notes

- Dependency pins are audited after every upstream release (pin ↔ tag ↔
  import-time validation must agree across `pyproject.toml`,
  `requirements*.txt`, and installed provenance). In the monorepo the pins
  keep their published meaning; `[tool.uv.sources]` redirects are dev-only.
