# `api/` — The web server (FastAPI)

## What this folder is (plain English)

This is the **front door** to Mailroom. It's a small web server (FastAPI) that lets you interact with the pipeline over HTTP, without touching files or the database directly. It runs on `http://localhost:8000`.

You use it to:

- Upload a document (`POST /upload`) — drops it in the inbox for the watcher to process.
- Check where a document is in the pipeline (`GET /status/{doc_id}`).
- Approve/reject documents that landed in human review (`POST /review/{doc_id}/resolve`).
- See the tamper-proof audit trail (`GET /audit/{doc_id}`).
- List everything in a matter (`GET /matters/{matter_id}`).
- See pipeline health/metrics (`GET /ops/status`, `GET /health`).

## Getting started

```bash
python api/main.py        # serves on http://localhost:8000
```

Then open `http://localhost:8000/docs` for an interactive test page (Swagger UI).

## Technical reference

- Single module: `main.py` defines `app = FastAPI(...)`. `python api/main.py` runs `uvicorn.run(app, host="0.0.0.0", port=8000)`. Equivalent: `uvicorn api.main:app --port 8000`.
- `lifespan` calls `_ensure_dirs()` on startup so the pipeline bins exist.
- `POST /upload` writes bytes straight into the inbox bin — it does NOT run the pipeline itself. Processing happens asynchronously in the watcher process. Response is `202 Accepted`.
- `GET /status` and `GET /matters` read from the Postgres/SQLite catalog via `storage/catalog.py`, falling back to the JSON manifest on DB failure.
- `GET /audit/{doc_id}` returns the hash chain from `storage/audit_log.py` plus a `chain_valid` bool from `schemas/audit.py:verify_chain`.
- `POST /ops/pause` / `POST /ops/resume` — manual ingestion pause/resume (mirrors the scheduled `pipeline/ops_monitor.py` sweep); `GET /ops/status` reports `paused_ingestion`.
- Full endpoint docs (request/response shapes): `docs/api.md`.

### Wiring notes

- The API shares `storage/` and `pipeline/bins.py` with the rest of the app, so the DB file and bins are the same ones the watcher uses.
- There is no auth on any endpoint — access-control is left to you (see root README → Security).
