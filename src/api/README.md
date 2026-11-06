# `api/` — The web server (FastAPI)

## What this folder is (plain English)

This is the **front door** to Mailroom. It's a small web server (FastAPI) that lets you interact with the pipeline over HTTP, without touching files or the database directly. It runs on `http://localhost:8000`.

You use it to:

- Upload a document (`POST /upload`) — drops it in the inbox for the watcher to process. The upload carries a tracking `upload_id` and honors the submitted `matter_id` via a `<file>.meta` sidecar the watcher reads. FastAPI defaults omitted or empty multipart `matter_id` fields to `DEFAULT`; whitespace-only and path-bearing identifiers are rejected with HTTP 400. Direct adapter calls also reject an empty identifier.
- See the live inbox → processing queue (`GET /queue`) — queued uploads (with their metadata), in-flight worker claims, and recent documents.
- Check where a document is in the pipeline (`GET /status/{doc_id}`).
- Approve/reject/record/requeue/complete documents on human review (`POST /review/{doc_id}/resolve`, dispositions from The-Mailroom PR #18/#20 — `doc_type` / `doc_subclass` class correction); list the tray (`GET /review/queue`), look up by trace/filename (`GET /lookup`), and read the parked file (`GET /documents/{doc_id}/source`, `?download=1` for original bytes).
- See the tamper-proof audit trail (`GET /audit/{doc_id}`) or summarize the whole local audit DB (`GET /audit`).
- List everything in a matter (`GET /matters/{matter_id}`).
- See pipeline health/metrics (`GET /ops/status`, `GET /health` — `/health` reports `checks.watcher` live/stale/missing plus how recently the watcher heartbeat was touched, i.e. whether uploads are actually being drained). `/ops/status` includes `first_pass` / `first_pass_rate`: documents that archived in one hop with no reroute, scored without ground truth.

## Getting started

```bash
python api/main.py        # serves on http://localhost:8000
# reachable producer for The-Mailroom Observatory (PR #30):
#   docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build
# then MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
#      MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
#      MAILROOM_PIPELINE_API_PREFIX=/v1
# Space pair: deploy/space/PAIRING.md (Inbox Queue → POST /v1/upload)
```

Then open `http://localhost:8000/docs` for an interactive test page (Swagger UI)
with Try-it-out buttons for every route. Day-to-day Inbox **Queue a document**
and REVIEW actions (Approve / Reject / Requeue, class correction, Open original)
should go through The-Mailroom's Observatory when `MAILROOM_PIPELINE_URL` +
`MAILROOM_PIPELINE_TOKEN` + `MAILROOM_PIPELINE_API_PREFIX=/v1` point here — no
typed endpoints required.
## Technical reference

- Single module: `main.py` defines `app = FastAPI(...)`. `python -m api.main`
  binds via `listen_host()` / `listen_port()`: host `MAILROOM_API_HOST`
  (default `127.0.0.1`); port from platform `PORT` when non-empty after stripping whitespace, else
  `MAILROOM_API_PORT` (default `8000`), so `PORT` wins on Fly/Render/any host.
  It also refuses an off-loopback bind without `MAILROOM_API_TOKEN` /
  `MAILROOM_API_TOKENS` (`assert_bind_allowed`, audit L-2), offers the pager
  (`--pager` / `--no-pager`) and sets up process tracing.
- `uvicorn api.main:app` serves the same app but does none of that: its listener ignores
  `PORT` / `MAILROOM_API_HOST` / `MAILROOM_API_PORT` (the app still reads
  `MAILROOM_API_TOKEN(S)`; pass `--host` / `--port` yourself; `UVICORN_HOST` /
  `UVICORN_PORT` also apply), skips the startup bind guard (with no live token, token-protected routes then
  answer 503 to requests arriving on a non-loopback address or carrying forwarding
  headers; the health checks and `/docs`, `/redoc`, `/openapi.json` stay open;
  behind any proxy set a token, since one that adds no forwarding header looks local) and starts no pager (run `python -m pipeline.pager run` or
  the watchdog separately if alerts are on). Prefer `python -m api.main` on hosts
  that inject `PORT`; for a proxy outside loopback set `MAILROOM_API_HOST=0.0.0.0`
  together with a token.
- `lifespan` calls `_ensure_dirs()` on startup and, unless `MAILROOM_EMBED_WATCHER=0`, starts the inbox watcher in-process (`watcher.lock` so a dedicated `python -m pipeline.watcher` cannot double-drain).
- `POST /upload` writes bytes straight into the inbox bin — it does NOT run the pipeline itself. Processing happens asynchronously in the (embedded or standalone) watcher. Response is `202 Accepted`, with an `upload_id` and the accepted `matter_id`. It also writes a `<file>.meta` sidecar (matter_id, upload_id, uploaded_at, size) that the watcher reads to file the document under the submitted matter.
- `GET /queue` lists queued inbox files (with sidecar metadata), in-flight `processing/<worker>/` claims, and recent catalog documents.
- `GET /status` and `GET /matters` read from the Postgres/SQLite catalog via `storage/catalog.py`, falling back to the JSON manifest on DB failure.
- `GET /audit/{doc_id}` returns the hash chain from `storage/audit_log.py` plus a `chain_valid` bool from `schemas/audit.py:verify_chain`.
- `POST /ops/resume` — clear the ingestion-pause flag (there is no `/ops/pause` endpoint; the pause flag is written by the ops monitor / operator); `GET /ops/status` reports `paused_ingestion` and pipeline-wide metrics.
- Full endpoint docs (request/response shapes): `docs/api.md`.
- Hosted: Hugging Face Docker Space; see [the deployment guide](../../docs/pipeline-reference-llm-mailroom/deployment/README.md) § Other container hosts (Railway retired).

### Wiring notes

- The API shares `storage/` and `pipeline/bins.py` with the rest of the app, so the DB file and bins are the same ones the watcher uses.
- Auth: every endpoint except `GET /health` requires the `MAILROOM_API_TOKEN` bearer token when one is configured (loopback-only dev works without; off-loopback / Spaces / container hosts **require** a token: `python -m api.main` exits without one, while a direct `uvicorn` start without one answers 503 on every token-protected route off loopback — see root README → Security).
