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

# Observability (optional) — Langfuse cloud, Langfuse self-hosted, Braintrust,
# or the local cost-free Arize Phoenix backend.
# OBSERVABILITY_PROVIDER=auto picks Langfuse when a secret key is set, else
# Braintrust when its key is set, else the local Phoenix backend (no cloud, no
# tokens — the default fallback).
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# Cloud: LANGFUSE_HOST=https://us.cloud.langfuse.com
# Self-hosted: LANGFUSE_HOST=http://localhost:3000 (LANGFUSE_BASE_URL is an alias)
# Alternative backends:
#   OBSERVABILITY_PROVIDER=braintrust + BRAINTRUST_API_KEY
#   OBSERVABILITY_PROVIDER=phoenix  (local; PHOENIX_ENDPOINT, run `phoenix serve`)

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
docker compose -f src/config/docker/docker-compose.yml up -d postgres
python -c "import asyncio; from storage.db import init_db; asyncio.run(init_db())"
```

---

## 4. Run Services

Start all services (each in its own terminal or use a process manager):

```bash
# Terminal 1: API (embeds the inbox watcher by default)
PYTHONPATH=src python -m api.main

# Terminal 2 (optional): dedicated watcher — only if MAILROOM_EMBED_WATCHER=0
PYTHONPATH=src python -m pipeline.watcher

# Terminal 3 (optional): Ops Monitor (system health sweeps)
PYTHONPATH=src python -m pipeline.ops_monitor
```

Uploads land in the inbox and drain while a watcher is running. `python -m api.main` starts that watcher in-process unless `MAILROOM_EMBED_WATCHER=0` (use that when a dedicated `python -m pipeline.watcher` already holds `watcher.lock`). `GET /health` reports `checks.watcher` (`live` / `stale` / `missing`) and `inbox_pending` so The-Mailroom's live floor can show operator liveness without fabricating document rows.

---

## 5. Verify Pipeline

```bash
# Upload a test document (returns upload_id; the watcher mints the doc_id
# once processing starts — see it in /queue or the watcher logs)
curl -X POST http://localhost:8000/upload \
  -F "file=@src/tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=TEST-001"

# Check status (use the doc_id once processing has started)
curl http://localhost:8000/status/<doc_id>

# View the queue (uploaded/processing/recent docs, incl. upload_id tracking)
curl http://localhost:8000/queue

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

**Arize Phoenix (local, cost-free):** start it with `phoenix serve` (or `python -m phoenix.server.main serve`), then open `http://localhost:6006` and confirm traces appear as documents flow. This is the default fallback in `auto` mode — no cloud subscription, no quota, nothing spent on top of the LLM API calls. The Phoenix SQLite DB can be deleted when a batch is done (pour-in, poke-around, discard).

Every LLM call (classification, extraction, reports, Boss) is auto-traced; no per-node wiring is needed.

---

## Production Considerations

### Process Management

Use `systemd`, `supervisord`, or Docker to manage the three processes:

```
[Service] pipeline-watcher  → PYTHONPATH=src python -m pipeline.watcher
[Service] mailroom-api      → PYTHONPATH=src uvicorn api.main:app --host 0.0.0.0 --port 8000
[Service] ops-monitor       → PYTHONPATH=src python -m pipeline.ops_monitor
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

## Docker Deployment (producer for The-Mailroom)

The-Mailroom Observatory ([PR #30](https://github.com/Exios66/The-Mailroom/pull/30))
needs a **reachable** llm-mailroom producer for the floor lamp, Inbox
**Queue a document** (`POST /v1/upload`), and REVIEW resolve
(`MAILROOM_PIPELINE_URL` + `MAILROOM_PIPELINE_TOKEN` +
`MAILROOM_PIPELINE_API_PREFIX=/v1`). The visualizer proxies operator
clicks here; the browser never holds the token. Two Hub Spaces — this
producer and The-Mailroom `mailroom-observatory` — are documented in
[`deploy/space/PAIRING.md`](../deploy/space/PAIRING.md).

### Local (laptop pair)

Set `MAILROOM_API_TOKEN` in `.env`, then:

```bash
docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build
```

The producer image is a multi-stage build that runs as non-root (`mailroom`, uid 10001), ships a `HEALTHCHECK` against `/health`, and the compose file sets `security_opt: no-new-privileges` plus `user: 10001:10001`. Langfuse compose secrets come from `.env` (`:?` required expansion) with pinned image tags. `PYTHONPATH=src python src/scripts/publish_space.py --check` asserts the Dockerfile baseline before publishing.

On The-Mailroom:

```bash
MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
MAILROOM_PIPELINE_API_PREFIX=/v1
```

Try-it-out without the visualizer: `http://127.0.0.1:8000/docs`.
`GET /health` reports `producer` / `review_resolve` plus the watcher lamp.

### Hugging Face Docker Space (hosted Observatory)

The root `Dockerfile` binds `0.0.0.0:7860` and refuses to start without
`MAILROOM_API_TOKEN`. Publish (keys stay in the environment, never the
Space git tree):

```bash
PYTHONPATH=src python src/scripts/publish_space.py --check
HF_TOKEN=hf_... MAILROOM_API_TOKEN=change-me \
  PYTHONPATH=src python src/scripts/publish_space.py --repo Lucius-Morningstar/mailroom-producer
```

Live Observatory (probed 2026-08-30):
[`Lucius-Morningstar/mailroom-observatory`](https://huggingface.co/spaces/Lucius-Morningstar/mailroom-observatory)
at `https://lucius-morningstar-mailroom-observatory.hf.space`. The matching
producer Space was **not** on the Hub at that probe — publish it, then set
The-Mailroom Space secrets to
`https://lucius-morningstar-mailroom-producer.hf.space` and the same token.
Keep the producer Space **public** so the Observatory can HTTP-call it; gate
every non-health route with the bearer token. Space disk under `/data` is
ephemeral — use the compose volume (or a VPS) when parked REVIEW files
must survive sleep.

See [`deploy/space/SPACE_README.md`](../deploy/space/SPACE_README.md).

---

## Backup & Restore

The audit log is the compliance record — backup strategy is a critical concern. The following guidance covers the SQLite default; the same principles apply to Postgres.

### What to back up

| Artifact | Path | Purpose | Frequency |
|---|---|---|---|
| Catalog DB | `data/mailroom.db` | matters, documents, audit_log | Daily (or continuous) |
| Crash-resume checkpoints | `data/checkpoints.db` | LangGraph in-flight state | Daily |
| Archived documents | `data/archive/` | Final durable document copies | Continuous (as docs are archived) |
| Manifests | `data/manifests/` | Self-contained per-document records (mirror of manifest JSON) | Daily |
| Mirrored run logs | `data/langfuse_logs/` | Offline analysis copies of traces | Optional — only if you use `sync_langfuse_logs.py` |

### SQLite backup

SQLite files are safe to copy with a consistent snapshot. **Do not** copy a live `.db` file while the watcher/API are writing to it without a safe snapshot mechanism:

```bash
# Recommended: use SQLite's online backup (safe while the service is running)
sqlite3 data/mailroom.db ".backup 'backup/mailroom.db'"
sqlite3 data/checkpoints.db ".backup 'backup/checkpoints.db'"

# Or, stop services, then plain copy:
# (stop watcher + API + ops monitor)
cp data/mailroom.db backup/
cp data/checkpoints.db backup/
```

Schedule a daily snapshot via cron:

```cron
# Daily 2am — safe online snapshot
0 2 * * * cd /path/to/llm-mailroom && \
  mkdir -p backup/$(date +\%Y-\%m-\%d) && \
  sqlite3 data/mailroom.db ".backup 'backup/$(date +\%Y-\%m-\%d)/mailroom.db'" && \
  sqlite3 data/checkpoints.db ".backup 'backup/$(date +\%Y-\%m-\%d)/checkpoints.db'" && \
  cp -R data/archive backup/$(date +\%Y-\%m-\%d)/archive && \
  cp -R data/manifests backup/$(date +\%Y-\%m-\%d)/manifests
```

Retain a rotation window (e.g. 30–90 days) sized to your compliance requirements. The audit log is append-only — backups are the only way to reconstruct it.

### Postgres backup

If using `DATABASE_URL` with Postgres, use `pg_dump`:

```bash
pg_dump -h localhost -U mailroom mailroom > backup/mailroom-$(date +%F).sql
```

### Restore procedure

1. Stop the watcher, API, and ops monitor (prevents writes during restore).
2. Restore the catalog DB:
   ```bash
   # SQLite
   cp backup/mailroom.db data/mailroom.db
   cp backup/checkpoints.db data/checkpoints.db
   # Postgres
   # psql -h localhost -U mailroom mailroom < backup/mailroom-YYYY-MM-DD.sql
   ```
3. Restore `/archive` and `/manifests`:
   ```bash
   cp -R backup/archive data/archive
   cp -R backup/manifests data/manifests
   ```
4. Restart services.
5. **Verify the audit chain**: `curl http://localhost:8000/audit/<doc_id>` must report `"chain_valid": true`. If hashes break, the restored DB and manifests are out of sync (e.g. mixed backup dates).

### Disaster-recovery checklist

- [ ] Archives + manifests + catalog DB backed up from the same point in time
- [ ] Audit chain verified after every restore
- [ ] Backups stored off-host (cloud object storage, WORM bucket, etc.)
- [ ] Test a restore at least quarterly — an untested backup is not a backup
- [ ] Encrypt backups at rest (they contain confidential client documents)

### Logging & Log Rotation

The pipeline emits **structured logs to stdout** (structlog, `LOG_FORMAT=json|pretty`, level `LOG_LEVEL`) — it does not write log files itself. Log file capture, rotation, and retention are the responsibility of the process manager (systemd, supervisord, Docker). Recommended policies:

| Concern | Recommendation |
|---|---|
| **Capture** | Redirect each service's stdout/stderr to a log file (see examples below) |
| **Rotation** | Rotate daily or at 100MB, whichever comes first |
| **Retention** | Keep 14–30 days (or as required by your retention policy); the audit log in SQLite is the long-term compliance record, logs are operational only |
| **Format** | Use `LOG_FORMAT=json` in production so rotated logs are machine-parseable |

**systemd** (`journald` handles rotation automatically):

```ini
[Service]
ExecStart=/usr/bin/PYTHONPATH=src python -m pipeline.watcher
StandardOutput=journal
StandardError=journal
```

**supervisord:**

```ini
[program:watcher]
command=/usr/bin/PYTHONPATH=src python -m pipeline.watcher
stdout_logfile=/var/log/mailroom/watcher.log
stdout_logfile_maxbytes=100MB
stdout_logfile_backups=14
stderr_logfile=/var/log/mailroom/watcher.err.log
stderr_logfile_maxbytes=100MB
stderr_logfile_backups=14
```

**logrotate** (if you redirect output to files manually):

```
/var/log/mailroom/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

**JSON logs + rotation:** when `LOG_FORMAT=json`, each line is a self-contained JSON object — safe to rotate at any line boundary, no partial-line concerns.

---

## Railway

Root [`railway.json`](../railway.json) forces the **Dockerfile** builder (multi-stage,
non-root, `HEALTHCHECK`) and probes `GET /health`. A [`nixpacks.toml`](../nixpacks.toml)
exists only as a fallback if a service is accidentally left on Nixpacks.

### Why deploys looked “crashed”

1. **Wrong listen port.** The image defaults `MAILROOM_API_PORT=7860` (Spaces).
   Railway injects `$PORT` and proxies to that port. The API prefers
   `PORT` over `MAILROOM_API_PORT` so the process binds where the edge expects.
2. **Missing bearer token.** `MAILROOM_API_HOST=0.0.0.0` (image default) refuses
   to start without `MAILROOM_API_TOKEN` / `MAILROOM_API_TOKENS` (audit L-2).
   Without the variable the process exits immediately → Railway `CRASHED`.

### Required service variables

| Variable | Value |
|---|---|
| `MAILROOM_API_HOST` | `0.0.0.0` (already in the image) |
| `MAILROOM_API_TOKEN` | shared secret (The-Mailroom `MAILROOM_PIPELINE_TOKEN` must match) |
| `OPENROUTER_API_KEY` | production LLM key |

Recommended: Langfuse keys under `auto`, `MAILROOM_BASE_DIR=/data` (attach a
volume for durable SQLite), `LOG_FORMAT=json`. On Railway, `auto` skips the
local Phoenix fallback unless `PHOENIX_ENDPOINT` is a remote collector.

### Deploy

```bash
railway link   # once
railway variables set MAILROOM_API_TOKEN=... OPENROUTER_API_KEY=...
railway up -m "mailroom producer"
# The-Mailroom:
#   MAILROOM_PIPELINE_URL=https://<your-railway-domain>
#   MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
#   MAILROOM_PIPELINE_API_PREFIX=/v1
```

Generate a public domain in the Railway dashboard (or `railway domain`) and
point The-Mailroom at it. Health: `curl -sS https://<domain>/health`.

---

## Troubleshooting

### Railway deploy CRASHED / restart loop

- Confirm `MAILROOM_API_TOKEN` is set on the **service** (not only shared).
- Confirm runtime logs show `Uvicorn running on http://0.0.0.0:<PORT>` where
  `<PORT>` matches Railway’s injected `PORT` (not stuck on 7860).
- Confirm `railway.json` is on the deployed branch (`builder: DOCKERFILE`).

### Watcher not picking up files

- Check that `MAILROOM_BASE_DIR` points to an existing directory
- Verify file extension is in the accepted list (`config/taxonomy.yaml` → `file_extensions`)
- Check watcher logs for errors

### Database errors

- **SQLite:** verify the `data/` directory is writable; the DB files are created automatically. If the DB was created by a different `MAILROOM_BASE_DIR`, point it back or delete the old files.
- **Postgres:** verify `DATABASE_URL` in `.env` and that Postgres is running: `docker compose -f src/config/docker/docker-compose.yml ps`

### Langfuse not showing traces

- Check `OBSERVABILITY_PROVIDER` — must be `auto` or `langfuse`
- Check `LANGFUSE_HOST` is correct (cloud: `https://us.cloud.langfuse.com`)
- For self-hosted: verify the Langfuse container is healthy and API keys in `.env` match the Langfuse UI project settings
- The pipeline runs without Langfuse — it degrades gracefully

### Braintrust not showing traces

- Check `OBSERVABILITY_PROVIDER=braintrust` and `BRAINTRUST_API_KEY`/`BRAINTRUST_PROJECT` are set
- Braintrust is a no-op until the API key is present

### No traces in `auto` mode after dropping cloud keys

With no `LANGFUSE_SECRET_KEY` or `BRAINTRUST_API_KEY`, `auto` falls through to the
local Arize Phoenix backend (cost-free) **on a laptop**. On Railway, local Phoenix
is skipped (no collector). To see traces:
- Start Phoenix locally: `phoenix serve`, then open `http://localhost:6006`
- Verify `PHOENIX_TRACING` is not `disabled` and `PHOENIX_ENDPOINT` matches Phoenix
- Set `OBSERVABILITY_PROVIDER=phoenix` explicitly if you want to force it
- Set `OBSERVABILITY_PROVIDER=none` only if you want tracing fully off
- On Railway, set Langfuse keys (or a remote `PHOENIX_ENDPOINT`)

### LLM provider errors

- OpenRouter: verify `OPENROUTER_API_KEY` and check usage/credits at openrouter.ai
- Ollama: verify the model is pulled (`ollama pull qwen3:7b`) and the service is running
- Check `DEFAULT_PROVIDER` env var isn't accidentally overriding your intended provider
