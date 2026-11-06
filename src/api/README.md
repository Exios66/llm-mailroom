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

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:b04fb68051764f5535fc1748eec36e2eedb6889b0dacd3d592db3abfeddb2398 -->

- Single module: `main.py` defines `app = FastAPI(...)`. `python -m api.main`
  binds via `listen_host()` / `listen_port()`: host `MAILROOM_API_HOST`
  (default `127.0.0.1`); port from platform `PORT` when non-empty after stripping whitespace, else
  `MAILROOM_API_PORT` (default `8000`), so `PORT` wins on Fly/Render/any host.
  When the host proxy connects from outside loopback and `MAILROOM_API_HOST` is
  unset, set `MAILROOM_API_HOST=0.0.0.0` and configure `MAILROOM_API_TOKEN`
  before starting the module. It refuses an off-loopback bind without a live
  `MAILROOM_API_TOKEN` / `MAILROOM_API_TOKENS` (`assert_bind_allowed`, audit L-2),
  offers the pager (`--pager` / `--no-pager`) and sets up process tracing.
- `uvicorn api.main:app` serves the same app. Its listener ignores
  `PORT` / `MAILROOM_API_HOST` / `MAILROOM_API_PORT`: pass `--host` / `--port`
  yourself (`UVICORN_HOST` / `UVICORN_PORT` also apply). Shared lifespan startup
  checks `MAILROOM_API_HOST` against the live tokens, including revocations.
  Because ASGI startup cannot inspect Uvicorn's `--host`, protected routes also
  reject requests on a non-loopback listener with HTTP 503 when no live token
  exists, even with `--lifespan off`. A CLI-only off-loopback bind therefore
  need not exit at startup. Configure a live token before launching and match
  `MAILROOM_API_HOST` to `--host` to get the startup guard as well.
  Plain Uvicorn starts no pager or process tracing. Matching the module's bind
  settings does not start the pager: when paging is enabled, run the watchdog
  or `PYTHONPATH=src python -m pipeline.pager run` separately for alerts to be sent.
  The module starts its own pager when its pager configuration is active.


<!-- /coderabbit-ai-generated:v1 -->

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
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:413b1f6221967ca962dc26475502c99ef093bf12e462a885f1f733f3b9978d48 -->

- Auth: every endpoint except `GET /health` requires a live bearer token when one is configured. Tokenless access to protected routes is allowed only on loopback. Module startup and shared lifespan reject configured off-loopback binds without a live token; direct Uvicorn CLI overrides are additionally guarded per request (HTTP 503 without a live token). See root README → Security.

<!-- /coderabbit-ai-generated:v1 -->
