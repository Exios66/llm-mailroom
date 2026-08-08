# Deployment Guide

## Prerequisites

- Python 3.11+
- OpenRouter API key (or a local LLM)
- Docker (optional — only needed for Langfuse tracing and/or local LLMs)
- 8GB+ RAM (16GB+ recommended for local model inference)

---

## 1. Clone and Configure

```bash
git clone <repo-url> llm-mailroom
cd llm-mailroom
cp .env.example .env
```

Edit `.env` with your values:

```bash
# Required for OpenRouter (primary provider)
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Database — SQLite by default (no server needed).
# The file is created automatically at {MAILROOM_BASE_DIR}/mailroom.db.
# To use Postgres instead, uncomment:
# DATABASE_URL=postgresql+asyncpg://mailroom:mailroom@localhost:5432/mailroom

# Observability (optional) — Langfuse cloud, Langfuse self-hosted, or Braintrust.
# OBSERVABILITY_PROVIDER=auto picks Langfuse when a secret key is set.
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# Cloud: LANGFUSE_HOST=https://us.cloud.langfuse.com
# Self-hosted: LANGFUSE_HOST=http://localhost:3000 (LANGFUSE_BASE_URL is an alias)
# Alternative backend: set OBSERVABILITY_PROVIDER=braintrust + BRAINTRUST_API_KEY

# Pipeline
MAILROOM_BASE_DIR=./data
```

---

## 2. Install Application

```bash
pip install -e ".[dev]"
```

---

## 3. Database

**Nothing to do** — SQLite tables are auto-created on first use. You'll see
`data/mailroom.db` (catalog + audit log) and `data/checkpoints.db` (crash-resume
state) appear after the first document is processed.

If you opted for Postgres, start it and initialize:

```bash
docker compose -f docker/docker-compose.yml up -d postgres
python -c "import asyncio; from storage.db import init_db; asyncio.run(init_db())"
```

---

## 4. Run Services

Start all services (each in its own terminal or use a process manager):

```bash
# Terminal 1: Pipeline Watcher (processes documents from inbox)
python pipeline/watcher.py

# Terminal 2: API Server
python api/main.py

# Terminal 3 (optional): Ops Monitor (system health sweeps)
python pipeline/ops_monitor.py
```

---

## 5. Verify Pipeline

```bash
# Upload a test document
curl -X POST http://localhost:8000/upload \
  -F "file=@tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=TEST-001"

# Check status (use the doc_id from upload response)
curl http://localhost:8000/status/<doc_id>

# View audit trail
curl http://localhost:8000/audit/<doc_id>

# Check pipeline health
curl http://localhost:8000/ops/status
```

---

## 6. Verify Observability (optional)

**Langfuse cloud:** open your project dashboard at `us.cloud.langfuse.com` and confirm traces appear as documents flow through the pipeline.

**Langfuse self-hosted:** open `http://localhost:3000` in your browser. Set up your first user account, generate API keys, and put them in `.env` (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`).

**Braintrust:** set `OBSERVABILITY_PROVIDER=braintrust` + `BRAINTRUST_API_KEY` and check your Braintrust project's logs.

Every LLM call (classification, extraction, reports, Boss) is auto-traced; no per-node wiring is needed.

---

## Production Considerations

### Process Management

Use `systemd`, `supervisord`, or Docker to manage the three processes:

```
[Service] pipeline-watcher  → python pipeline/watcher.py
[Service] mailroom-api      → uvicorn api.main:app --host 0.0.0.0 --port 8000
[Service] ops-monitor       → python pipeline/ops_monitor.py
```

### Database

- **Default:** a local SQLite file (`data/mailroom.db`). Back it up along with `data/checkpoints.db` and `/archive`.
- The audit log is append-only — size will grow over time.
- For higher volume or multi-process setups, switch to Postgres via `DATABASE_URL` and consider partitioning `audit_log` by date for long-term retention.

### Security

- Encrypt `/archive` at rest and the SQLite files at rest (filesystem encryption, cloud KMS, etc.)
- Access-control the FastAPI endpoints (API keys, OAuth, or network-level)
- Access-control the Langfuse UI (it exposes full document content in traces)
- Do not expose Postgres or ClickHouse ports publicly (if you run them for Langfuse)
- Back up `/archive` and the audit log table independently

### Scaling

For pilot scale (dozens of documents/day):
- The current architecture (threaded watcher, single process) is sufficient
- SQLite handles the concurrency comfortably at this scale

For higher volumes:
- Consider Redis-based queuing (deferred per the roadmap)
- Multiple watcher workers with distinct worker IDs (claim mechanism already handles this)
- Load-balance the API behind a reverse proxy

### Monitoring

- Langfuse: live trace viewer for LLM call latency, token usage, error rates
- `/ops/status`: pipeline-level metrics (stuck docs, review backlog, error rates)
- Ops monitor: automated periodic sweeps with Boss agent analysis
- Standard infrastructure monitoring for Postgres, ClickHouse, disk usage on `/archive`

---

## Docker Deployment (Full Stack)

A production Docker setup would include the application as a service:

```yaml
# Example addition to docker-compose.yml (not included by default)
services:
  mailroom-api:
    build: .
    command: python api/main.py
    ports:
      - "8000:8000"
    environment:
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
      # SQLite by default — data lives in the volume below.
      - MAILROOM_BASE_DIR=/data
    volumes:
      - mailroom_data:/data
```

---

## Troubleshooting

### Watcher not picking up files

- Check that `MAILROOM_BASE_DIR` points to an existing directory
- Verify file extension is in the accepted list (`config/taxonomy.yaml` → `file_extensions`)
- Check watcher logs for errors

### Database errors

- **SQLite:** verify the `data/` directory is writable; the DB files are created automatically. If the DB was created by a different `MAILROOM_BASE_DIR`, point it back or delete the old files.
- **Postgres:** verify `DATABASE_URL` in `.env` and that Postgres is running: `docker compose -f docker/docker-compose.yml ps`

### Langfuse not showing traces

- Check `OBSERVABILITY_PROVIDER` — must be `auto` or `langfuse`
- Check `LANGFUSE_HOST` is correct (cloud: `https://us.cloud.langfuse.com`)
- For self-hosted: verify the Langfuse container is healthy and API keys in `.env` match the Langfuse UI project settings
- The pipeline runs without Langfuse — it degrades gracefully

### Braintrust not showing traces

- Check `OBSERVABILITY_PROVIDER=braintrust` and `BRAINTRUST_API_KEY`/`BRAINTRUST_PROJECT` are set
- Braintrust is a no-op until the API key is present

### LLM provider errors

- OpenRouter: verify `OPENROUTER_API_KEY` and check usage/credits at openrouter.ai
- Ollama: verify the model is pulled (`ollama pull qwen3:7b`) and the service is running
- Check `DEFAULT_PROVIDER` env var isn't accidentally overriding your intended provider
