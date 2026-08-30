# Pair The-Mailroom Observatory with this producer

The-Mailroom [PR #30](https://github.com/Exios66/The-Mailroom/pull/30) (and
the earlier REVIEW-resolve PRs) talks to **this** API. The visualizer never
holds producer keys in the browser. Two Hugging Face Docker Spaces make the
floor + inbox + REVIEW desk reachable from the Hub:

| Space | Repo | Process | Port | Role |
|---|---|---|---|---|
| **Observatory** (floor) | The-Mailroom `mailroom-observatory` | `python -m server.hosted` | 7860 | Langfuse-only display + server-side proxy |
| **Producer** (pipeline) | this repo `mailroom-producer` | `python -m api.main` | 7860 | Inbox, watcher, REVIEW resolve, catalog |

`http://127.0.0.1:8000` only works when the visualizer and producer share a
host. A Space Observatory **must** use the public producer Space URL —
loopback inside the Observatory container is not this API.

## Three knobs (The-Mailroom process)

Set these on the **visualizer** (laptop `.env` or Observatory Space
**secrets** / variables). They are not read by this producer.

```bash
MAILROOM_PIPELINE_URL=https://<user>-mailroom-producer.hf.space
# laptop pair instead:
# MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
MAILROOM_PIPELINE_API_PREFIX=/v1
```

| Visualizer knob | Producer equivalent |
|---|---|
| `MAILROOM_PIPELINE_URL` | This API's public origin (no path). Space: `https://<user>-mailroom-producer.hf.space` |
| `MAILROOM_PIPELINE_TOKEN` | `MAILROOM_API_TOKEN` (or a live key from `MAILROOM_API_TOKENS`) |
| `MAILROOM_PIPELINE_API_PREFIX` | `/v1` (unversioned aliases still exist) |

Do **not** point `MAILROOM_API_URL` at the producer. That knob is TUI →
visualizer `:8001`.

The-Mailroom `scripts/publish_space.py` copies URL + token as Space
**secrets** and `/v1` as a **variable** when they are in the environment.

## What the Observatory proxies here

| Observatory action | Producer route | Notes |
|---|---|---|
| Floor lamp | `GET /v1/health` | `checks.watcher`, `inbox_pending`, `producer`, `review_resolve`, `inbox_upload` |
| Queue a document | `POST /v1/upload` | Multipart `file` + optional `matter_id` → **202 Accepted** |
| Inbox / tray | `GET /v1/queue`, `GET /v1/review/queue` | Sidecars + parked REVIEW |
| Lookup | `GET /v1/lookup` | `doc_id` / `trace_id` / `filename` |
| Approve / Reject / Record / Requeue / Complete | `POST /v1/review/{doc_id}/resolve` | JSON body; `override_doc_type` from visualizer `doc_type` |
| Text pane / Open original | `GET /v1/documents/{doc_id}/source` | `?download=1` for bytes |
| Audit | `GET /v1/audit`, `GET /v1/audit/{doc_id}` | Hash chain |

Unconfigured visualizer returns an honest **503** — no fabricated catalog
row. Display envelopes stay Langfuse-only (`document-pipeline` traces).

## Bring both Spaces up

From **this** checkout (producer):

```bash
export MAILROOM_API_TOKEN="$(openssl rand -hex 24)"
PYTHONPATH=src python src/scripts/publish_space.py --check
HF_TOKEN=hf_... MAILROOM_API_TOKEN=$MAILROOM_API_TOKEN \
  OPENROUTER_API_KEY=sk-or-... \
  LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
  LANGFUSE_HOST=https://us.cloud.langfuse.com \
  PYTHONPATH=src python src/scripts/publish_space.py --repo <user>/mailroom-producer
```

From **The-Mailroom** checkout (Observatory / floor):

```bash
export MAILROOM_PIPELINE_URL=https://<user>-mailroom-producer.hf.space
export MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
export MAILROOM_PIPELINE_API_PREFIX=/v1
HF_TOKEN=hf_... \
  LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
  LANGFUSE_HOST=https://us.cloud.langfuse.com \
  MAILROOM_PIPELINE_URL=$MAILROOM_PIPELINE_URL \
  MAILROOM_PIPELINE_TOKEN=$MAILROOM_PIPELINE_TOKEN \
  python scripts/publish_space.py --repo <user>/mailroom-observatory
```

Then open `https://huggingface.co/spaces/<user>/mailroom-observatory`.
Queue a document and REVIEW buttons hit the producer Space; cards and the
conveyor read Langfuse. Observatory-side cache knobs
(`MAILROOM_TRACE_CACHE_DIR`, `MAILROOM_POLL_ENRICH=inflight`) stay on the
visualizer — this producer does not set them.

## Laptop pair

```bash
# this repo
docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build
# The-Mailroom
MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
MAILROOM_PIPELINE_API_PREFIX=/v1
mailroom-hosted   # Observatory at http://127.0.0.1:8001/live
```

## Security

- Producer Space stays **public** so the Observatory can HTTP-call it.
- Every producer route except `GET /health` requires the bearer token.
- Never put tokens in Space **Variables** (plain text to collaborators).
- Producer `/data` on Spaces is **ephemeral**. Use
  `deploy/docker-compose.producer.yml` (or a VPS) when parked REVIEW files
  must survive sleep.
