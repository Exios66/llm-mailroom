# Getting Started

## Prerequisites

- Python 3.11+
- OpenRouter API key ([get one here](https://openrouter.ai/keys))
- Docker (optional — only for Langfuse tracing and local LLMs)
- 8GB+ RAM

## Step 1: Clone and Configure

Standalone checkout (lightest path — this page's steps below):

```bash
git clone https://github.com/Exios66/llm-mailroom
cd llm-mailroom
cp .env.example .env
```

Inside the **monorepo** ([mailroom-dev](https://github.com/Exios66/mailroom-dev)),
this repo is already present as `packages/llm-mailroom` (git subtree) and the
whole constellation installs with one `uv sync` at the monorepo root — skip
Step 2's `pip install` and run `PYTHONPATH=src python -m api.main` from
`packages/llm-mailroom`. The monorepo is the source of truth for
cross-repository development; see
[`docs/sister-repos.md`](https://github.com/Exios66/llm-mailroom/blob/main/docs/sister-repos.md).

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

Open two terminals (the API embeds the inbox watcher by default):

**Terminal 1 — API Server:**
```bash
PYTHONPATH=src python -m api.main
```

Set `MAILROOM_EMBED_WATCHER=0` and start `PYTHONPATH=src python -m pipeline.watcher` only when you want a dedicated watcher process.

**Terminal 2 — Ops Monitor (optional):**
```bash
PYTHONPATH=src python -m pipeline.ops_monitor
```

## Step 4: Process a Document

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@src/tests/fixtures/contract/sample_msa.txt" \
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

If you configured observability, every LLM call is auto-traced — `OBSERVABILITY_PROVIDER=auto` resolves Langfuse if its key is set, else Braintrust if its key is set, else the local cost-free Arize Phoenix (`PHOENIX_TRACING`, no subscription needed). Open your backend's dashboard to see prompts, responses, latency, and token usage. The pipeline runs fine without any tracing.

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
| `MAILROOM_API_TOKEN` | Off-loopback | — |

The-Mailroom Observatory (sister visualizer, [PR #30](https://github.com/Exios66/The-Mailroom/pull/30)) does **not** read this `.env` for display — Langfuse is its source. Inbox **Queue a document** and REVIEW resolve need three knobs **on the visualizer**:

```
MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
# Space Observatory: https://lucius-morningstar-mailroom-producer.hf.space
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
MAILROOM_PIPELINE_API_PREFIX=/v1
```

Pairing checklist: [deploy/space/PAIRING.md](https://github.com/Exios66/llm-mailroom/blob/main/deploy/space/PAIRING.md).

---

## Next Steps

- [Repository docs/](https://github.com/Exios66/llm-mailroom/tree/main/docs) — architecture, agents, configuration, and more
- [Sister Repositories](https://github.com/Exios66/llm-mailroom/blob/main/docs/sister-repos.md) — the llm-mailroom umbrella map
