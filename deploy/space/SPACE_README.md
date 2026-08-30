---
title: Mailroom Producer
emoji: 📨
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Reachable llm-mailroom API for The-Mailroom floor + REVIEW
---

# Mailroom Producer

Hosted [llm-mailroom](https://github.com/Exios66/llm-mailroom) API — the
**producer** [The-Mailroom](https://github.com/Exios66/The-Mailroom)
Observatory ([PR #30](https://github.com/Exios66/The-Mailroom/pull/30))
needs for Inbox **Queue a document**, REVIEW resolve, and the floor lamp.

This Space is **not** the Observatory and **not** the pixel console. The
floor lives on a second Space (`<user>/mailroom-observatory`). This
image serves FastAPI (`python -m api.main`) on **7860**. Pair the floor
with the three visualizer knobs (not read here):

```
MAILROOM_PIPELINE_URL=https://<user>-mailroom-producer.hf.space
MAILROOM_PIPELINE_TOKEN=<same value as this Space's MAILROOM_API_TOKEN>
MAILROOM_PIPELINE_API_PREFIX=/v1
```

`127.0.0.1:8000` is the laptop producer only — it is unreachable from a
Space Observatory. Checklist: [PAIRING.md](PAIRING.md).

`GET /health` is open (`producer` / `review_resolve` / `inbox_upload` plus
the watcher lamp). Every other route requires
`Authorization: Bearer $MAILROOM_API_TOKEN`. The browser never holds that
token — The-Mailroom proxies `POST /api/inbox/enqueue` → `POST /v1/upload`
and REVIEW → `POST /v1/review/{doc_id}/resolve`.

## Hugging Face dashboard

| Setting | Value |
|---|---|
| SDK | **Docker** (not Gradio / Streamlit / static) |
| Root directory | Space repo root (the committed `Dockerfile`) |
| App port | **7860** |
| Hardware | CPU basic is enough (no GPU — models are called via OpenRouter) |
| Visibility | **Public** so the Observatory can HTTP-call it; keep keys as **Secrets** |

### Secrets (Settings → Variables and secrets → Secrets)

| Name | Notes |
|---|---|
| `MAILROOM_API_TOKEN` | **Required.** Off-loopback bind refuses to start without it. Same value as The-Mailroom `MAILROOM_PIPELINE_TOKEN`. |
| `OPENROUTER_API_KEY` | Needed for `resume` (re-extract). `record` / `requeue` / `complete` work without it. |
| `LANGFUSE_PUBLIC_KEY` | Optional. Live-floor traces for The-Mailroom. |
| `LANGFUSE_SECRET_KEY` | Optional. Never a regular variable. |
| `LANGFUSE_HOST` | Production US cloud: `https://us.cloud.langfuse.com` |

### Variables (optional, not secret)

| Name | Default |
|---|---|
| `MAILROOM_API_HOST` | `0.0.0.0` (already set in the Dockerfile) |
| `MAILROOM_API_PORT` | `7860` |
| `MAILROOM_EMBED_WATCHER` | `1` |
| `OBSERVABILITY_PROVIDER` | `auto` |

Do **not** put tokens in Variables (visible to Space collaborators as plain
text). Disk under `/data` is **ephemeral** on Spaces — bins and SQLite reset
when the Space sleeps. Use a durable host (`deploy/docker-compose.producer.yml`
or a VPS) when REVIEW must keep parked files across restarts.

## Republish

From the GitHub checkout (keys stay in the environment):

```bash
pip install huggingface_hub
HF_TOKEN=hf_... \
  MAILROOM_API_TOKEN=change-me \
  OPENROUTER_API_KEY=sk-or-... \
  LANGFUSE_PUBLIC_KEY=pk-lf-... \
  LANGFUSE_SECRET_KEY=sk-lf-... \
  LANGFUSE_HOST=https://us.cloud.langfuse.com \
  PYTHONPATH=src python src/scripts/publish_space.py --repo <user>/mailroom-producer
```

`--check` validates the Docker payload without calling the Hub.
