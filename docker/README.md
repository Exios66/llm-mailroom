# `docker/` — Optional services

## What this folder is (plain English)

Mailroom itself needs **no Docker** — the code runs with plain Python and stores everything in SQLite files under `data/`.

Docker is only needed for two **optional** extras:

1. **Langfuse** (`langfuse-server` + its two backends, `postgres` and `clickhouse`) — a web UI at `http://localhost:3000` that shows every LLM call as a trace (input/output, latency, tokens). Great for debugging, but the pipeline runs fine without it.
2. **Ollama** — runs local LLMs (Qwen, Llama, Mistral, …) if you want to stop paying for cloud models. Turned off by default; enable with the `local-llm` profile.

## Start it

```bash
docker compose -f docker/docker-compose.yml up -d postgres clickhouse langfuse-server   # Langfuse
docker compose -f docker/docker-compose.yml --profile local-llm up -d ollama            # local LLM
docker compose -f docker/docker-compose.yml ps                                          # check health
```

## Technical reference

- `docker-compose.yml` services:
  | Service | Port(s) | Purpose |
  |---|---|---|
  | `postgres` (pgvector/pg16) | 5432 | Backend for Langfuse **and** optional Mailroom storage |
  | `clickhouse` | 8123/9000 | Analytics backend for Langfuse |
  | `langfuse-server` | 3000 | Trace viewer UI |
  | `ollama` (profile-gated) | 11434 | Local LLM for `DEFAULT_PROVIDER=ollama` |
- **Postgres is now optional for Mailroom.** Storage defaults to SQLite (`data/mailroom.db`). To use Postgres for Mailroom instead, uncomment `DATABASE_URL` in `.env` and start the `postgres` service.
- Ollama is gated behind `profiles: [local-llm]` so it isn't started by default (and reserves a GPU). Pull models with `docker exec mailroom-ollama ollama pull qwen3:7b`.
- Volumes are named (`pgdata`, `clickhouse_data`, `ollama_data`) so data survives `docker compose down`.
- Langfuse first-run (self-hosted): create an account at `http://localhost:3000`, generate API keys, and put them in `.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`). You can skip Docker entirely for tracing by using Langfuse **cloud** (`LANGFUSE_HOST=https://us.cloud.langfuse.com`) or **Braintrust** (`OBSERVABILITY_PROVIDER=braintrust`) — see `observability/README.md`.
- `docker/volumes/` is gitignored — any bind-mounted data goes there.
