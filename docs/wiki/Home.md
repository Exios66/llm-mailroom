# Mailroom — Multi-Agent Legal Document Processing Pipeline

Welcome to the Mailroom wiki.

Mailroom is a multi-agent pipeline that ingests high-volume legal documents, classifies them, routes them to specialist agents for extraction, compiles the results into a matter record, and archives everything with a full, tamper-evident audit trail.

**v1 targets pilot scale** (dozens of documents/day), organized by case/matter, running on [OpenRouter](https://openrouter.ai) with a clear path to fully local inference.

---

## Design Principles

1. **Auditability over cleverness** — Every classification, extraction, and routing decision must be traceable.
2. **Explicit over emergent** — Orchestration is a defined LangGraph state machine.
3. **Human-legible state** — Filesystem bins mean anyone can `ls` a folder and understand where a document is.
4. **Provider-agnostic LLM layer** — OpenRouter today, local models later with one config change.
5. **Redundant record-keeping** — The audit trail does not depend on any single tool staying alive.

---

## Quick Links

| Page | Description |
|---|---|
| [Home](Home) | This page |
| [Getting Started](Getting-Started) | Installation and first run |
| [Architecture](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/architecture.md) | Full architectural overview |
| [Configuration](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/configuration.md) | Config reference and environment variables |
| [Agents](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/agents.md) | Agent specifications and personalities |
| [API Reference](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/api.md) | Complete API endpoint documentation |
| [Deployment](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/deployment/README.md) | Production deployment guide |
| [Local Model Cutover](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/local-models.md) | Switching to local LLMs |
| [Development](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/testing.md) | Development and testing guide |
| [FAQ](FAQ) | Frequently asked questions |
| [Sister Repositories](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/sister-repos.md) | The llm-mailroom umbrella: entity-extraction, llm-dojo-scoring, corpus feeds |

---

## Architecture at a Glance

```
Upload/Drop --> /pipeline/inbox/ --> [Watcher] --> LangGraph run per document
                                                        |
              Sorter --> Specialist --> [Judge/Arbiter gate] --> Reporter --> Catalog --> Archivist
                                                        |
                                    Boss (escalation)    Human Review    Audit Log
```

**13 LangGraph nodes** in a state machine (`intake`, `classify`, `retry_classify`, `review_classify`, `extract`, `retry_extract`, `judge_verify`, `arbiter`, `human_review`, `boss_escalation`, `compile_report`, `catalog_write`, `archive`) — including the exception lanes from the architecture-alignment build: an agent second-opinion reviewer for exhausted medium-band classifications (Lane A) and a gated judge→arbiter completeness-verification path for grounded extractions (Lane B). Checkpointing is in-memory by default (stateless design; review resume re-invokes from the manifest) with opt-in `SqliteSaver` via `MAILROOM_CHECKPOINTER=sqlite`.

## Quick Start

```bash
docker compose -f src/config/docker/docker-compose.yml up -d postgres clickhouse langfuse-server   # OPTIONAL: Langfuse tracing only
cp .env.example .env
pip install -e ".[dev]"
PYTHONPATH=src python -m api.main &
# optional dedicated watcher when MAILROOM_EMBED_WATCHER=0:
# PYTHONPATH=src python -m pipeline.watcher &
curl -X POST http://localhost:8000/upload -F "file=@src/tests/fixtures/contract/sample_msa.txt" -F "matter_id=MATTER-001"
```

## The Mailroom Umbrella

**[Digital-Mailroom](https://github.com/LLM-Mailroom-Services/Digital-Mailroom)** is the integration monorepo: one `uv` workspace importing committed core packages as git subtrees (`packages/llm-mailroom` consumes this standalone repo), with the hub task board (`governance/TASKS.md`) and sync driver (`scripts/sync_packages.py status|pull`). Author and test package implementation and documentation in the owning standalone repository first, then commit and merge to its own `main` before Digital-Mailroom imports the committed source with `scripts/sync_packages.py pull`. The hub owns integration wiring, governance, and generated vendor snapshots.

Mailroom is the pipeline at the center of that governed constellation. DMR-079 left seven monorepo members; [DMR-093](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/issues/165) defines the **five-package core**: `llm-dojo-scoring`, `llm-mailroom`, `local-mailroom-sandbox`, `The-Mailroom`, and `mailroom-corpus-eda`. This is the target until the separate hub pruning PR lands after [#162](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/pull/162); hub `main` retains seven members in the meantime. The wider constellation still includes: **[llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring)** (the pinned scoring engine, `v0.24.1`), external corpus feeds in the DMR-093 target **[Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment)** (correspondence) and **[claims-data-eda](https://github.com/Exios66/claims-data-eda)** (insurance claims, candidate), eval sibling **[atticus-investigation](https://github.com/Exios66/atticus-investigation)** (LegalBench), the downstream visualizer **[The-Mailroom](https://github.com/Exios66/The-Mailroom)** (pixel-art document conveyor + hosted Observatory Space, driven solely by this repo's Langfuse traces; Inbox and REVIEW resolve need this API as `MAILROOM_PIPELINE_URL`). Retired from the core in DMR-079 and no longer synced: `llm-entity-extraction`, `agent-mailroom` and the `llm-mailroom-graph` knowledge-graph sites (see the Retired section of the Sister Repositories map). Full map: [docs/sister-repos.md](https://github.com/Exios66/llm-mailroom/blob/main/docs/pipeline-reference-llm-mailroom/sister-repos.md). Two-Space pair: [deploy/space/PAIRING.md](https://github.com/Exios66/llm-mailroom/blob/main/deploy/space/PAIRING.md).

DMR-093 preserves the two feeder package paths in the full hub checkout at commit `59c879d9`: [Enron](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/enron-evaluation-environment/packages/Enron-Evaluation-Environment) and [claims](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/claims-data-eda/packages/claims-data-eda). Both refs are published; final PR162 must retain their exact feeder package trees before hub pruning. Their standalone repositories, dataset provenance, and guides remain available. The five-package target has four built members and one virtual member, `mailroom-corpus-eda`.
