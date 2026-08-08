# Mailroom — Multi-Agent Legal Document Processing Pipeline

Mailroom is a multi-agent pipeline that ingests high-volume legal documents for a transactional/corporate practice, classifies them, routes them to specialist agents for extraction, compiles the results into a matter record, and archives everything with a full audit trail.

---

## Quick Start

> **No database server needed.** Mailroom now stores everything (catalog + audit log + crash-resume checkpoints) in a plain **SQLite file** inside your data folder. If you don't already use Docker, you can ignore it entirely.

```bash
# 1. Configure
cp .env.example .env
# Edit .env — add your OPENROUTER_API_KEY

# 2. Install
pip install -e ".[dev]"

# 3. (Optional) Start Langfuse for trace viewing — needs Docker
docker compose -f docker/docker-compose.yml up -d postgres clickhouse langfuse-server

# 4. Run the watcher (starts processing documents from inbox)
python pipeline/watcher.py

# 5. In another terminal, start the API
python api/main.py

# 6. Upload a document
curl -X POST http://localhost:8000/upload \
  -F "file=@tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=MATTER-001"

# 7. Check pipeline status
curl http://localhost:8000/status/{doc_id}

# 8. View full audit trail
curl http://localhost:8000/audit/{doc_id}
```

When a document is processed, you'll get two files under `data/`:
- `data/mailroom.db` — the SQLite database (matters, documents, audit_log tables)
- `data/checkpoints.db` — LangGraph crash-resume state

## Architecture

```
Upload/Drop → /pipeline/inbox/ → [Watcher] → LangGraph run per document
                                                  │
                    Sorter → Specialist → Reporter → Catalog → Archivist
                                                  │
                    Boss (escalation)    Human Review    Audit Log
```

**11 LangGraph nodes** in an SQLite-checkpointed state machine. One graph execution per document, resumable across crashes/restarts.

## Design Principles

1. **Auditability over cleverness.** Every classification, extraction, and routing decision is traceable.
2. **Explicit over emergent.** Orchestration is a defined state machine — agents don't freely negotiate.
3. **Human-legible state.** Filesystem bins let anyone `ls` a folder and understand where a document is.
4. **Provider-agnostic LLM layer.** OpenRouter today, local models later — one config change.
5. **Redundant record-keeping.** Audit trail doesn't depend on any single tool staying alive.

## Project Structure

```
mailroom/
├── agents/          # Specialist agents (Sorter, Contract, Corp Records, etc.)
├── graph/           # LangGraph state machine: nodes, routing, state
├── llm/             # Provider-agnostic LLM client (OpenRouter, Ollama, vLLM)
├── schemas/         # Pydantic models: manifest, matter, documents, audit
├── pipeline/        # Watcher, filesystem bins, ops monitor
├── storage/         # SQLite/Postgres: catalog CRUD, audit log
├── api/             # FastAPI: upload, review, status, audit
├── observability/   # Langfuse callback handlers
├── config/          # taxonomy.yaml — doc classes, thresholds, model mappings
├── docker/          # docker-compose: Langfuse, Ollama (Postgres optional)
├── tests/           # pytest: unit, routing, e2e, fixtures
└── docs/            # Detailed documentation
```

## Configuration

All config lives in `config/taxonomy.yaml` — **never hardcoded**:

```yaml
# Add a doc class:
doc_classes:
  - key: new_doc_type
    label: "New Document Type"
    schema: NewExtractionSchema
    specialist: new_specialist

# Adjust thresholds:
confidence:
  high: 0.85
  low: 0.70       # below this → retry → still low → human review
  retry_max: 1    # max retries before routing to review

# Per-agent model mapping (cut over agent-by-agent):
agents:
  sorter:
    provider: openrouter
    model: openai/gpt-4o
```

## LLM Providers

| Provider | Status | Auth | Base URL |
|---|---|---|---|
| **OpenRouter** | Primary | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| **Ollama** | Local | None | `http://localhost:11434/v1` |
| **vLLM** | Local | None | `http://localhost:8000/v1` |
| **Generic** | Fallback | `GENERIC_API_KEY` | Configurable |

Global override: set `DEFAULT_PROVIDER=ollama` in `.env`.

## Local Model Cutover (Phase 10)

```bash
# See current agent→model assignments
python cutover.py --list

# Move sorter to local (safest first step)
python cutover.py --agent sorter --provider ollama --model qwen3:7b

# Validate with tests
python cutover.py --validate --agent sorter

# View recommended cutover order
python cutover.py --recommend

# Cut all agents at once
python cutover.py --all --provider ollama --model qwen3:7b
```

### Available Local Models (Ollama)

| Model | Sizes | Best For |
|---|---|---|
| Qwen 3 | 7b, 14b | Structured output, legal text extraction |
| Qwen 2.5 | 14b, 32b | Multilingual support |
| Llama 3.1 | 8b, 70b | General-purpose, reliable structured output |
| Llama 3.2 | 3b | Lightweight classification |
| Mistral | 7b | Fast instruction following |
| Mistral Nemo | 12b | Speed/quality balance |
| Mixtral | 8x7b | Strong extraction (MoE) |
| DeepSeek-R1 | 8b, 14b | Legal reasoning and analysis |
| Phi-4 | 14b | Document understanding |
| Gemma 2 | 9b, 27b | Instruction following |
| Command R | 35b, 104b | RAG and extraction |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/upload` | Upload document to inbox |
| `POST` | `/review/{doc_id}/resolve` | Resolve human review (approved/rejected) |
| `GET` | `/status/{doc_id}` | Document pipeline status |
| `GET` | `/matters/{matter_id}` | All documents in a matter |
| `GET` | `/audit/{doc_id}` | Hash-chained audit trail + validity check |
| `GET` | `/ops/status` | Pipeline-wide operational metrics |

## Pipeline Bins (Filesystem)

```
data/
  pipeline/
    inbox/               # New uploads land here
    processing/<id>/     # Claimed by worker (atomic rename)
    classified/<type>/   # Sorted, pending specialist
    review/              # Human review required
    failed/              # Unrecoverable errors
  archive/
    <matter_id>/<type>/  # Final durable home
  manifests/
    <doc_id>.json        # Mirror of DocumentManifest
  mailroom.db            # SQLite: matters, documents, audit_log
  checkpoints.db         # LangGraph crash-resume state
```

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test suites
pytest tests/test_agents/ -v
pytest tests/test_routing.py -v
pytest tests/test_audit_log.py -v
pytest tests/test_pipeline_e2e.py -v

# With coverage
pytest tests/ --cov=. --cov-report=html
```

## Pilot Testing & Evaluation

A ready-made set of 12 legal PDFs lives in `examples/samples/` (real SEC-exhibit
contracts from the CC-BY-4.0 [CUAD](https://huggingface.co/datasets/theatticusproject/cuad)
dataset plus original text for the other doc classes). Use them to pilot the
pipeline and **measure the effect of procedural changes** on accuracy and
efficiency:

```bash
# Build the sample PDFs into data/samples/ (gitignored)
python scripts/prepare_samples.py

# Deterministic run (fake LLM, no API key) — tests the machinery
python scripts/run_pilot.py --mock

# Real run (needs OPENROUTER_API_KEY in .env) — measures LLM accuracy too
python scripts/run_pilot.py --real

# Diff two runs, e.g. after a routing/threshold change
python scripts/run_pilot.py --mock --baseline data/pilot_report.json
```

The report records per-document stage, doc type, confidence, retries, LLM call
count, and wall time, and scores each against the ground truth in
`examples/samples/manifest.csv`. See `examples/samples/README.md`.

## Deployment

```bash
# 1. (Optional) Start Langfuse for trace viewing
docker compose -f docker/docker-compose.yml up -d postgres clickhouse langfuse-server

# 2. Set environment
export OPENROUTER_API_KEY=sk-or-v1-...
# MAILROOM_BASE_DIR defaults to ./data; mailroom.db + checkpoints.db are created there automatically

# 3. Run the pipeline watcher
python pipeline/watcher.py &

# 4. Run the API server
python api/main.py &

# 5. (Optional) Run the ops monitor
python pipeline/ops_monitor.py &
```

## Observability

- **Tracing** — every LLM call (prompt, response, tokens, latency) is auto-logged to **Langfuse** (cloud `us.cloud.langfuse.com` or self-hosted) or **Braintrust**, selected via `OBSERVABILITY_PROVIDER` in `.env`. Optional — the pipeline runs fine with tracing disabled.
- **Audit log** — append-only, SHA-256 hash-chained entries in SQLite (tamper-evident)
- **Manifest sidecar** — JSON file archived alongside every document (self-contained record)

## Security

- Encrypt `/archive` at rest and the SQLite files (`mailroom.db`, `checkpoints.db`) at rest
- Access-control the FastAPI endpoints and the Langfuse UI
- Back up `/archive` and the audit log table independently
- Treat retention policy as an open decision — not assumed by this system

## Further Documentation

- [Architecture](docs/architecture.md) — full architectural details
- [Configuration](docs/configuration.md) — config reference
- [Agents](docs/agents.md) — agent specifications and personalities
- [API Reference](docs/api.md) — complete API documentation
- [Deployment](docs/deployment.md) — deployment and operations
- [Testing](docs/testing.md) — testing strategy and fixtures
- [Local Models](docs/local-models.md) — local model cutover guide
- [Wiki](https://github.com/your-org/llm-mailroom/wiki) — GitHub wiki
