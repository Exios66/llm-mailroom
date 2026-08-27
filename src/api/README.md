# `api/` — The web server (FastAPI)

## What this folder is (plain English)

This is the **front door** to Mailroom. It's a small web server (FastAPI) that lets you interact with the pipeline over HTTP, without touching files or the database directly. It runs on `http://localhost:8000`.

You use it to:

- Upload a document (`POST /upload`) — drops it in the inbox for the watcher to process. The upload carries a tracking `upload_id` and honors the submitted `matter_id` via a `<file>.meta` sidecar the watcher reads.
- See the live inbox → processing queue (`GET /queue`) — queued uploads (with their metadata), in-flight worker claims, and recent documents.
- Check where a document is in the pipeline (`GET /status/{doc_id}`).
- Approve/reject documents that landed in human review (`POST /review/{doc_id}/resolve`).
- See the tamper-proof audit trail (`GET /audit/{doc_id}`).
- List everything in a matter (`GET /matters/{matter_id}`).
- See pipeline health/metrics (`GET /ops/status`, `GET /health` — `/health` reports `checks.watcher` live/stale/missing plus how recently the watcher heartbeat was touched, i.e. whether uploads are actually being drained).

## Getting started

```bash
python api/main.py        # serves on http://localhost:8000
```

Then open `http://localhost:8000/docs` for an interactive test page (Swagger UI).

## Technical reference

- Single module: `main.py` defines `app = FastAPI(...)`. `python api/main.py` runs `uvicorn.run(app, host="0.0.0.0", port=8000)`. Equivalent: `uvicorn api.main:app --port 8000`.
- `lifespan` calls `_ensure_dirs()` on startup and, unless `MAILROOM_EMBED_WATCHER=0`, starts the inbox watcher in-process (`watcher.lock` so a dedicated `python -m pipeline.watcher` cannot double-drain).
- `POST /upload` writes bytes straight into the inbox bin — it does NOT run the pipeline itself. Processing happens asynchronously in the (embedded or standalone) watcher. Response is `202 Accepted`, with an `upload_id` and the accepted `matter_id`. It also writes a `<file>.meta` sidecar (matter_id, upload_id, uploaded_at, size) that the watcher reads to file the document under the submitted matter.
- `GET /queue` lists queued inbox files (with sidecar metadata), in-flight `processing/<worker>/` claims, and recent catalog documents.
- `GET /status` and `GET /matters` read from the Postgres/SQLite catalog via `storage/catalog.py`, falling back to the JSON manifest on DB failure.
- `GET /audit/{doc_id}` returns the hash chain from `storage/audit_log.py` plus a `chain_valid` bool from `schemas/audit.py:verify_chain`.
- `POST /ops/resume` — clear the ingestion-pause flag (there is no `/ops/pause` endpoint; the pause flag is written by the ops monitor / operator); `GET /ops/status` reports `paused_ingestion` and pipeline-wide metrics.
- Full endpoint docs (request/response shapes): `docs/api.md`.

### Wiring notes

- The API shares `storage/` and `pipeline/bins.py` with the rest of the app, so the DB file and bins are the same ones the watcher uses.
- Auth: all endpoints except `GET /health` and `GET /matters/{matter_id}` require the `MAILROOM_API_TOKEN` bearer token when one is configured (loopback-only dev works without; see root README → Security).
