# Deployment Guide

## Prerequisites

- Python 3.11+
- Docker and Docker Compose
- OpenRouter API key (or a local LLM)
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

# Database
DATABASE_URL=postgresql+asyncpg://mailroom:mailroom@localhost:5432/mailroom
DATABASE_URL_SYNC=postgresql+psycopg://mailroom:mailroom@localhost:5432/mailroom

# Langfuse (self-hosted)
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
LANGFUSE_HOST=http://localhost:3000

# Pipeline
MAILROOM_BASE_DIR=./data
```

---

## 2. Start Infrastructure

```bash
docker compose -f docker/docker-compose.yml up -d
```

This starts:
- **Postgres 16** — `localhost:5432`
- **ClickHouse** — `localhost:8123` (for Langfuse)
- **Langfuse Server** — `http://localhost:3000`

Wait for all services to be healthy:

```bash
docker compose -f docker/docker-compose.yml ps
```

---

## 3. Install Application

```bash
pip install -e ".[dev]"
```

---

## 4. Initialize Database

The database tables are auto-created on first use. To initialize manually:

```bash
python -c "import asyncio; from storage.db import init_db; asyncio.run(init_db())"
```

---

## 5. Run Services

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

## 6. Verify Pipeline

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

## 7. Verify Langfuse

Open `http://localhost:3000` in your browser. Set up your first user account. You'll see traces for every LLM call flowing through the pipeline.

> **Note:** For first-time Langfuse setup, create an account at `http://localhost:3000` and generate API keys. Update your `.env` with the generated public/secret keys.

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

- Use a managed Postgres instance or ensure proper backups
- The audit log is append-only — size will grow over time
- Consider partitioning `audit_log` by date for long-term retention

### Security

- Encrypt `/archive` at rest (filesystem encryption, cloud KMS, etc.)
- Enable Postgres encryption at rest (TDE or equivalent)
- Access-control the FastAPI endpoints (API keys, OAuth, or network-level)
- Access-control the Langfuse UI (it exposes full document content in traces)
- Do not expose Postgres or ClickHouse ports publicly
- Back up `/archive` and the audit log table independently

### Scaling

For pilot scale (dozens of documents/day):
- The current architecture (threaded watcher, single process) is sufficient
- Postgres handles the concurrency comfortably

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
      - DATABASE_URL=postgresql+asyncpg://mailroom:mailroom@postgres:5432/mailroom
      - MAILROOM_BASE_DIR=/data
    volumes:
      - mailroom_data:/data
    depends_on:
      postgres:
        condition: service_healthy
```

---

## Troubleshooting

### Watcher not picking up files

- Check that `MAILROOM_BASE_DIR` points to an existing directory
- Verify file extension is in the accepted list (`config/taxonomy.yaml` → `file_extensions`)
- Check watcher logs for errors

### Postgres connection errors

- Verify `DATABASE_URL` in `.env`
- Check Postgres is running: `docker compose -f docker/docker-compose.yml ps`
- Try the sync URL: `DATABASE_URL_SYNC=postgresql+psycopg://mailroom:mailroom@localhost:5432/mailroom`

### Langfuse not showing traces

- Check `LANGFUSE_HOST` is correct
- Verify Langfuse container is healthy
- Check Langfuse API keys match between `.env` and the Langfuse UI project settings
- The pipeline runs without Langfuse — it degrades gracefully

### LLM provider errors

- OpenRouter: verify `OPENROUTER_API_KEY` and check usage/credits at openrouter.ai
- Ollama: verify the model is pulled (`ollama pull qwen3:7b`) and the service is running
- Check `DEFAULT_PROVIDER` env var isn't accidentally overriding your intended provider
