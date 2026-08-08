# Deployment

## Prerequisites

- Python 3.11+
- Docker and Docker Compose
- OpenRouter API key (or local LLM)
- 8GB+ RAM (16GB+ for local models)

---

## 1. Infrastructure

```bash
docker compose -f docker/docker-compose.yml up -d
```

Services:
| Service | Port | Purpose |
|---|---|---|
| Postgres 16 | 5432 | Catalog, checkpointer, audit log |
| ClickHouse | 8123/9000 | Langfuse analytics |
| Langfuse | 3000 | Trace viewer UI |
| Ollama (profile) | 11434 | Local LLM (Phase 10) |

---

## 2. Configuration

```bash
cp .env.example .env
```

Critical variables:
```bash
OPENROUTER_API_KEY=sk-or-v1-...
DATABASE_URL=postgresql+asyncpg://mailroom:mailroom@localhost:5432/mailroom
DATABASE_URL_SYNC=postgresql+psycopg://mailroom:mailroom@localhost:5432/mailroom
```

---

## 3. Install

```bash
pip install -e ".[dev]"
```

---

## 4. Run Services

Three processes:

```bash
python pipeline/watcher.py &      # Pipeline processor
python api/main.py &              # FastAPI server
python pipeline/ops_monitor.py &  # Health sweeps (optional)
```

---

## 5. Verify

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=TEST-001"

curl http://localhost:8000/ops/status
```

---

## Production Considerations

### Process Management
Use systemd, supervisord, or Docker for the three processes.

### Database
- Use managed Postgres or ensure proper backups
- Audit log is append-only — partition by date for long retention

### Security
- Encrypt `/archive` at rest
- Enable Postgres encryption at rest
- Access-control FastAPI endpoints (API keys, OAuth, network)
- Access-control Langfuse UI (exposes full document content in traces)
- Never expose Postgres/ClickHouse ports publicly
- Back up `/archive` and audit log table independently

### Scaling (Pilot Scale)
- Threaded watcher + single process is sufficient for dozens/day
- For higher volume: Redis-based queuing (deferred), multiple workers

### Monitoring
- Langfuse: LLM call traces, latency, token usage
- `/ops/status`: Pipeline metrics
- Ops monitor: Automated periodic health sweeps

---

## Docker Deployment

Add application services to docker-compose:

```yaml
services:
  mailroom-api:
    build: .
    command: python api/main.py
    ports: ["8000:8000"]
    environment:
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
      - DATABASE_URL=postgresql+asyncpg://mailroom:mailroom@postgres:5432/mailroom
    volumes:
      - mailroom_data:/data
    depends_on:
      postgres:
        condition: service_healthy
```

---

## Troubleshooting

| Issue | Fix |
|---|---|
| Watcher not picking up files | Check `MAILROOM_BASE_DIR`, file extensions, watcher logs |
| Postgres connection errors | Verify `DATABASE_URL`, check Postgres is running |
| Langfuse no traces | Verify host, API keys, check container health |
| LLM provider errors | Check API key, credits, network connectivity |
