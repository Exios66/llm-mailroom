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
| [Architecture](Architecture) | Full architectural overview |
| [Configuration](Configuration) | Config reference and environment variables |
| [Agents](Agents) | Agent specifications and personalities |
| [API Reference](API-Reference) | Complete API endpoint documentation |
| [Deployment](Deployment) | Production deployment guide |
| [Local Model Cutover](Local-Model-Cutover) | Switching to local LLMs |
| [Development](Development) | Development and testing guide |
| [FAQ](FAQ) | Frequently asked questions |

---

## Architecture at a Glance

```
Upload/Drop --> /pipeline/inbox/ --> [Watcher] --> LangGraph run per document
                                                        |
                                    Sorter --> Specialist --> Reporter --> Catalog --> Archivist
                                                        |
                                    Boss (escalation)    Human Review    Audit Log
```

**11 LangGraph nodes** in an SQLite-checkpointed state machine. One graph per document, resumable across crashes.

## Quick Start

```bash
cp .env.example .env
pip install -e ".[dev]"
python pipeline/watcher.py &
python api/main.py &
curl -X POST http://localhost:8000/upload -F "file=@tests/fixtures/contract/sample_msa.txt" -F "matter_id=MATTER-001"
```

No database server needed — SQLite files (`data/mailroom.db`, `data/checkpoints.db`) are created automatically. Docker is only required for the optional Langfuse trace viewer (`docker compose -f docker/docker-compose.yml up -d postgres clickhouse langfuse-server`).
