# Getting Started

## Prerequisites

- Python 3.11+
- Docker and Docker Compose
- OpenRouter API key ([get one here](https://openrouter.ai/keys))
- 8GB+ RAM

## Step 1: Clone and Configure

```bash
git clone <repo-url>
cd llm-mailroom
cp .env.example .env
```

Edit `.env` with your OpenRouter key:

```bash
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

## Step 2: Start Infrastructure

```bash
docker compose -f docker/docker-compose.yml up -d
```

This starts Postgres, ClickHouse, and Langfuse. Verify:

```bash
docker compose -f docker/docker-compose.yml ps
```

All services should show `healthy` or `running`.

## Step 3: Install the Application

```bash
pip install -e ".[dev]"
```

## Step 4: Run Services

Open three terminals:

**Terminal 1 — Pipeline Watcher:**
```bash
python pipeline/watcher.py
```

**Terminal 2 — API Server:**
```bash
python api/main.py
```

**Terminal 3 — Ops Monitor (optional):**
```bash
python pipeline/ops_monitor.py
```

## Step 5: Process a Document

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=MATTER-001"
```

Watch the watcher terminal — you'll see the pipeline log each stage. The document moves through:
1. `inbox` → `processing` → `classified` → extracted → `archived`

## Step 6: Check Results

```bash
# Get document status (use the doc_id from the watcher output)
curl http://localhost:8000/status/<doc_id>

# View the full audit trail
curl http://localhost:8000/audit/<doc_id>

# See pipeline-wide metrics
curl http://localhost:8000/ops/status
```

## Step 7: Browse the Archive

```bash
ls -R data/archive/MATTER-001/
```

The document is now in its final home: `data/archive/MATTER-001/contract/sample_msa.txt`

## Step 8: View Langfuse Traces

Open `http://localhost:3000` in your browser. Set up your first user account.

You'll see traces for every LLM call: classification, extraction, reporting — with full input/output, latency, and token usage.

---

## Environment Variables Quick Reference

| Variable | Required | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | — |
| `DATABASE_URL` | No | `postgresql+asyncpg://mailroom:mailroom@localhost:5432/mailroom` |
| `LANGFUSE_HOST` | No | `http://localhost:3000` |
| `MAILROOM_BASE_DIR` | No | `./data` |
| `DEFAULT_PROVIDER` | No | `openrouter` |

---

## Next Steps

- [Configuration](Configuration) — customize taxonomy, thresholds, and model mappings
- [Architecture](Architecture) — understand the full system design
- [Agents](Agents) — learn about each specialist agent
