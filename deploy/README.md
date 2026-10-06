<div align="center">

# 🚀 Deployment Configuration

**Compose files, Modal app, and Space payload for llm-mailroom.**

</div>

---

Operator manuals for this folder are published on the GitBook site (pages listed in [`docs/SUMMARY.md`](../docs/SUMMARY.md) — a page not listed there is not on the site):

| Guide | Covers |
|:---|:---|
| [Docker deployment](../docs/docker-deployment.md) | Compose matrix (Modes B / Mixed / A-ollama / A-llamafile / M / G), producer image, BERT build args |
| [Modal + vLLM](../docs/modal-vllm.md) | `mailroom-vllm` serve, pipeline cutover, multi-tier Mode G GPUs |
| [Deployment](../docs/deployment.md) | Laptop Python install, Railway, Hugging Face Spaces, backup |

This README is an index of files in `deploy/`. Keep the operator recipes on the GitBook pages so they stay in sync.

## Structure

| Path | Contents |
|:---|:---|
| [`space/`](space/) | HuggingFace Space deployment |
| `modal_vllm.py` | Modal + vLLM serving app (`mailroom-vllm`) |
| `docker-compose.yml` | Base compose — `app` + optional `watcher` (Modes A / B / mixed / M) |
| `docker-compose.ollama.yml` | Mode A overlay: local Ollama sidecar |
| `docker-compose.llamafile.yml` | Mode A overlay: local llamafile sidecar |
| `docker-compose.full.yml` | Mode G — app, ops-monitor, watchdog, postgres, LiteLLM gateway, optional Phoenix |
| `docker-compose.producer.yml` | The-Mailroom REVIEW pairing (reachable producer) |
| `llamafile/` | llamafile sidecar image (pinned binary) + entrypoint |
| `models/` | Host staging for weights (GGUF / ModernBERT bundle) — never committed |

`MAILROOM_API_TOKEN` is **required** on every compose `app` service (the container binds `0.0.0.0`; audit L-2 refuses an off-loopback bind without a live bearer token). Secrets come from the host `.env` via `--env-file`. Never bake tokens into images.

Quick start (Mode B / Mixed): see [Docker deployment](../docs/docker-deployment.md). GPU serve: see [Modal + vLLM](../docs/modal-vllm.md).

## Configuration Files

| File | Purpose |
|:---|:---|
| `Dockerfile` (repository root) | Multi-stage producer / compose image |
| `nixpacks.toml` | Nixpacks fallback |
| `railway.json` | Railway deployment config |

## Usage

Build from the package directory:

```bash
cd packages/llm-mailroom
docker build -t llm-mailroom .
```

(Standalone checkout: `docker build -t llm-mailroom .` at the repo root.)

## Related Files

- [`docs/docker-deployment.md`](../docs/docker-deployment.md)
- [`docs/modal-vllm.md`](../docs/modal-vllm.md)
- [`docs/deployment.md`](../docs/deployment.md)
- `src/` — Source code
