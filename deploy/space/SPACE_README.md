---
title: Mailroom Producer
emoji: 📨
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Reachable llm-mailroom API for The-Mailroom REVIEW resolve
---

# Mailroom Producer

Hosted [llm-mailroom](https://github.com/Exios66/llm-mailroom) API — the
**producer** The-Mailroom REVIEW desk needs for Approve / Reject / Record /
Requeue / Complete.

This Space is **not** the Observatory and **not** the pixel console. It
serves FastAPI (`python -m api.main`) on **7860**. Point the visualizer at
it:

```
MAILROOM_PIPELINE_URL=https://<user>-mailroom-producer.hf.space
MAILROOM_PIPELINE_TOKEN=<same value as this Space's MAILROOM_API_TOKEN>
MAILROOM_PIPELINE_API_PREFIX=/v1
```

`GET /health` is open (watcher lamp + `producer` / `review_resolve` flags).
Every other route requires `Authorization: Bearer $MAILROOM_API_TOKEN`.
The browser never holds that token — The-Mailroom proxies from its server.

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
