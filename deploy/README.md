<div align="center">

# 🚀 Deployment Configuration

**Deployment configuration for the llm-mailroom package.**

</div>

---

## Structure

| Path | Contents |
|:---|:---|
| [`space/`](space/) | HuggingFace Space deployment |
| `modal_vllm.py` | Modal+vLLM serving app (optional local-model cutover; see below) |
| `docker-compose.yml` | Base compose — app + watcher services (Modes A/B/mixed/M share it) |
| `docker-compose.ollama.yml` | Mode-A overlay: local **Ollama** sidecar (offline LLM) |
| `docker-compose.llamafile.yml` | Mode-A overlay: local **llamafile** sidecar (offline LLM) |
| `llamafile/` | llamafile sidecar image (pinned binary) + entrypoint |
| `models/` | Host staging for weights (GGUF / ModernBERT bundle) — never committed |
| `docker-compose.producer.yml` | The-Mailroom REVIEW pairing (unchanged contract) |

## Docker Compose — deployment matrix

One image, three offline/online postures, selected by env + optional overlay:

| Mode | LLM source | Provider env | BERT intake | Invocation |
|:---|:---|:---|:---|:---|
| **B** — API | OpenRouter | `DEFAULT_PROVIDER=openrouter` + `OPENROUTER_API_KEY` | off | `docker compose -f deploy/docker-compose.yml --env-file .env up -d --build` |
| **Mixed** (dev default) | OpenRouter | as B | on (`MAILROOM_BERT_INTAKE=1`) | base file |
| **A-ollama** | Ollama sidecar | `DEFAULT_PROVIDER=ollama` + `OLLAMA_BASE_URL=http://ollama:11434/v1` | on | base + `-f deploy/docker-compose.ollama.yml` |
| **A-llamafile** | llamafile sidecar | `DEFAULT_PROVIDER=llamafile` + `LLAMAFILE_BASE_URL=http://llamafile:8080/v1` | on | base + `-f deploy/docker-compose.llamafile.yml` |
| **M** — Modal vLLM (remote GPU) | Modal app | `DEFAULT_PROVIDER=vllm` + `VLLM_BASE_URL=<modal.run>/v1` + `VLLM_API_KEY` | free | `modal deploy deploy/modal_vllm.py` + base file env |

```bash
# Mode A (fully offline) with Ollama:
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ollama.yml \
  --env-file .env up -d --build
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ollama.yml \
  exec ollama ollama pull qwen3:7b        # once; needs network

# Mode A with llamafile (see § llamafile below for model prep):
LLAMAFILE_MODEL=/models/llamafile/qwen3-7b-instruct-q4_k_m.gguf \
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.llamafile.yml \
  --env-file .env up -d --build
```

`MAILROOM_API_TOKEN` is **required** by the compose app service (the
container binds `0.0.0.0`; audit L-2 refuses an off-loopback bind without a
live bearer token). The API embeds the inbox watcher
(`MAILROOM_EMBED_WATCHER=1`); a `watcher` service is provided for
watcher-only deployments with `MAILROOM_EMBED_WATCHER=0` (watcher.lock is an
flock — exactly one intake authority per `/data`). Overlay runs pass
`--profile local-llm` so the sidecar's `profiles:` gate matches its
`depends_on` (compose validates against active profiles).

## Llamafile (Mozilla — single-file OpenAI-compatible server)

Prefer llamafile over Ollama when you want a zero-daemon, single-binary
GGUF server pinned to an exact release. The sidecar image bakes the pinned
binary (`deploy/llamafile/Dockerfile`, `LLAMAFILE_VERSION`, default 0.10.4);
the **GGUF weights are bind-mounted** from `deploy/models/llamafile/` (`:ro`)
— see `deploy/models/README.md`. Serve flags (entrypoint): `--server`,
`--jinja`, `--ctx-size`, `--mlock`, `-np 1`, `--gpu` (default `disable` =
CPU; `auto|nvidia|amd|apple|vulkan` on GPU hosts). The served model id is
the `--alias` value (`LLAMAFILE_ALIAS`), which must match
`taxonomy.yaml: llamafile_model_map:`.

Resource notes (CPU, no GPU anywhere in this stack): qwen3 **7B Q4_K_M ≈
4.7 GB** weights (≈ 6–8 GB RSS with a 16k-token KV cache), **14B ≈ 8.5 GB**
(≈ 11–13 GB RSS). A 7B-class model is the practical ceiling on a 16 GB
laptop; 27B-class only on GPU hosts. The model loads at server start and
stays resident (`--mlock`) — there is no unload-on-idle like Ollama's
`KEEP_ALIVE`; that is the memory-vs-latency trade of this path. Healthcheck:
`GET /health` (llama.cpp server JSON `{"status":"ok"}`).

## Offline model packaging (BERT fast-path)

The root `Dockerfile` is multi-stage (builder → **model** → runtime) and
ARGuable per mode:

| ARG | Default | Purpose |
|:---|:---|:---|
| `PIP_EXTRAS` | `bert` | Optional extras installed into the image (`""` = leanest Mode B image; the `[bert]` extra = onnxruntime + tokenizers + huggingface-hub) |
| `ML_EXPORT_ONNX` | `1` | Hook for the future int8 ONNX export pipeline (until that tooling ships, the raw safetensors+heads bundle is staged) |
| `ML_MODEL_REPO` | `Lucius-Morningstar/mailroom-modernbert-classifier` | Bundle source repo |
| `ML_MODEL_REVISION` | `main` | Pin a Hub commit SHA for reproducible builds |
| `ML_BUILD_NONE` | `0` | `1` = skip the model download (empty bundle dir; BERT lane fails open) |
| `FINAL_USER` | `mailroom` | Docker/Compose contract (uid 10001). **Modal builds must pass `root`** — SDK 1.5.5 has no `container_user` and appends its runtime after the Dockerfile |

Runtime contract: `ML_MODEL_DIR` (default `/models/mailroom-modernbert-classifier`)
+ `MAILROOM_BERT_INTAKE=0/1` (gate; off by default). The BERT lane in
`agents/bert_intake.py` fails **open** — missing package/bundle/model
degrades to the deterministic clerk, never crashes intake. The `[bert]`
extra contains **no torch**: runtime inference is onnxruntime CPU only
(verified wheels: onnxruntime 1.30.0, cp311, linux amd64 + aarch64,
`manylinux_2_28`, glibc ≥ 2.28 — bookworm is 2.36). `transformers`/`torch`/
`optimum` belong exclusively to the future build-time ONNX export tooling
and are deliberately NOT in `pyproject.toml`.

## Modal vLLM

`modal_vllm.py` runs vLLM's own OpenAI-compatible `/v1` server behind a Modal
`web_server` (SDK pinned `modal==1.5.5`, image `vllm/vllm-openai:v0.28.0` —
matches the local compose pin).

```bash
pip install -e ".[deploy]" && modal token new
export MODAL_VLLM_MODEL=Qwen/Qwen3-8B MODAL_VLLM_GPU=L4 MODAL_VLLM_API_TOKEN=<secret>
modal run deploy/modal_vllm.py::download_model   # pre-warm weights (CPU-only)
modal deploy deploy/modal_vllm.py                # prints the URL
export DEFAULT_PROVIDER=vllm VLLM_BASE_URL=<modal-url>/v1 VLLM_API_KEY=<token>
```

Knobs: `MODAL_VLLM_MODEL` / `MODAL_VLLM_GPU` / `MODAL_VLLM_MAX_MODEL_LEN` /
`MODAL_VLLM_GPU_MEMORY_UTILIZATION` / `MODAL_VLLM_MAX_NUM_SEQS` /
`MODAL_VLLM_TP_SIZE` (from the GPU `:N` suffix; DMR-051) /
`MODAL_VLLM_QUANTIZATION` / `MODAL_VLLM_REVISION` / `MODAL_VLLM_IMAGE_TAG` /
`MODAL_VLLM_API_TOKEN` / `MODAL_VLLM_SCALEDOWN_SECONDS` /
`MODAL_VLLM_STARTUP_TIMEOUT_SECONDS` + `HF_TOKEN` for gated weights — all
baked into a deploy-time `Secret.from_dict` (SDK 1.5.5 removed
`Secret.from_local`). Debug (DMR-053): `modal run deploy/modal_vllm.py --debug`
prints the masked resolved config, and `--check` probes `/models` with bearer
hints; `download_model` fails loudly on an empty snapshot. Bearer auth is
enforced by vLLM itself; the pipeline sends `VLLM_API_KEY` as the bearer
(`llm/providers.py`), and Modal scale-to-zero 503s get a long bounded
cold-start backoff in `llm/retry.py` (DMR-052).

## Configuration Files

| File | Purpose |
|:---|:---|
| `Dockerfile` | Docker image definition |
| `nixpacks.toml` | Nixpacks configuration |
| `railway.json` | Railway deployment config |

## Usage

Build from the package directory:
```bash
cd packages/llm-mailroom
docker build -t llm-mailroom .
```

## Related Files

- `src/` — Source code
- `docs/` — Documentation