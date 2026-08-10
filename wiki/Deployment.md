# Deployment

## Prerequisites

- Python 3.11+
- OpenRouter API key (or local LLM)
- Docker (optional — only for Langfuse tracing and/or local LLMs)
- 8GB+ RAM (16GB+ for local models)

---

## 1. Install

```bash
pip install -e ".[dev]"
```

## 2. Configuration

```bash
cp .env.example .env
```

Critical variables:
```bash
OPENROUTER_API_KEY=sk-or-v1-...
# Database is SQLite by default (data/mailroom.db) — no server needed.
# To use Postgres, set: DATABASE_URL=postgresql+asyncpg://mailroom:mailroom@localhost:5432/mailroom
```

---

## 3. Database

**Nothing to do** — SQLite tables auto-create on first use (`data/mailroom.db`,
`data/checkpoints.db`). For Postgres: `docker compose -f docker/docker-compose.yml up -d postgres`
then `python -c "import asyncio; from storage.db import init_db; asyncio.run(init_db())"`.

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
- **Default:** local SQLite file (`data/mailroom.db`). Back it up with `data/checkpoints.db` and `/archive`.
- Audit log is append-only — partition by date for long retention (Postgres)

### Security
- Encrypt `/archive` at rest and the SQLite files at rest
- Access-control FastAPI endpoints (API keys, OAuth, network)
- Access-control Langfuse UI (exposes full document content in traces)
- Never expose Postgres/ClickHouse ports publicly (if run for Langfuse)
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
      - MAILROOM_BASE_DIR=/data
    volumes:
      - mailroom_data:/data
```

---

## Logging & Log Rotation

The pipeline emits **structured logs to stdout** — it does not write log files itself. Capture, rotation, and retention are the process manager's job (systemd, supervisord, Docker).

- Set `LOG_FORMAT=json` in production for machine-parseable logs.
- Rotate daily or at 100MB; keep 14–30 days. The SQLite audit log is the long-term compliance record — rotated logs are operational only.

**supervisord example:**

```ini
[program:watcher]
command=/usr/bin/python pipeline/watcher.py
stdout_logfile=/var/log/mailroom/watcher.log
stdout_logfile_maxbytes=100MB
stdout_logfile_backups=14
```

See `docs/deployment.md` for systemd and logrotate examples.

---

## Backup & Restore

The audit log is the compliance record — back it up. See `docs/deployment.md` for the full guide.

**What to back up:** `data/mailroom.db` (catalog + audit log), `data/checkpoints.db` (crash-resume), `data/archive/` (final documents), `data/manifests/`.

**SQLite safe snapshot** (works while running):

```bash
sqlite3 data/mailroom.db ".backup 'backup/mailroom.db'"
sqlite3 data/checkpoints.db ".backup 'backup/checkpoints.db'"
```

**Restore:** stop services → restore the three artifacts → restart → verify the audit chain via `GET /audit/{doc_id}` (`"chain_valid": true`).

> Always back up archive + manifests + DB from the same point in time, store off-host, and test a restore quarterly.

---

## Troubleshooting

| Issue | Fix |
|---|---|---|
| Watcher not picking up files | Check `MAILROOM_BASE_DIR`, file extensions, watcher logs |
| Database errors | SQLite: check `data/` is writable. Postgres: verify `DATABASE_URL` and that the service is running |
| Langfuse/Braintrust no traces | Verify `OBSERVABILITY_PROVIDER` + keys/host, check dashboard |
| LLM provider errors | Check API key, credits, network connectivity |
