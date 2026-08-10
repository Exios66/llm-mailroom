# Getting Started

## Prerequisites

- Python 3.11+
- OpenRouter API key ([get one here](https://openrouter.ai/keys))
- Docker (optional — only for Langfuse tracing and local LLMs)
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

## Step 2: Install the Application

```bash
pip install -e ".[dev]"
```

No database setup needed — SQLite files are created automatically under `data/`
(`mailroom.db` for catalog + audit log, `checkpoints.db` for crash-resume).

## Step 3: Run Services

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

## Step 4: Process a Document

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@tests/fixtures/contract/sample_msa.txt" \
  -F "matter_id=MATTER-001"
```

Watch the watcher terminal — you'll see the pipeline log each stage. The document moves through:
1. `inbox` → `processing` → `classified` → extracted → `archived`

## Step 5: Check Results

```bash
# Get document status (use the doc_id from the watcher output)
curl http://localhost:8000/status/<doc_id>

# View the full audit trail
curl http://localhost:8000/audit/<doc_id>

# See pipeline-wide metrics
curl http://localhost:8000/ops/status
```

## Step 6: Browse the Archive

```bash
ls -R data/archive/MATTER-001/
```

The document is now in its final home: `data/archive/MATTER-001/contract/sample_msa.txt`

## Step 7: View LLM Traces (optional)

If you configured observability (Langfuse or Braintrust), every LLM call is auto-traced — open your Langfuse dashboard (`us.cloud.langfuse.com` or your self-hosted instance) or Braintrust project to see prompts, responses, latency, and token usage. The pipeline runs fine without any tracing.

---

## Environment Variables Quick Reference

| Variable | Required | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | — |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///<MAILROOM_BASE_DIR>/mailroom.db` |
| `OBSERVABILITY_PROVIDER` | No | `auto` |
| `LANGFUSE_HOST` | No | `http://localhost:3000` |
| `MAILROOM_BASE_DIR` | No | `./data` |
| `DEFAULT_PROVIDER` | No | `openrouter` |

---

## Next Steps

- [Repository docs/](https://github.com/Exios66/llm-mailroom/tree/main/docs) — architecture, agents, configuration, and more
