# Docker

Full compose matrix for llm-mailroom: OpenRouter, local Ollama/llamafile, BERT intake, the The-Mailroom producer image, and the single-host Mode G stack (LiteLLM gateway + Modal GPU tiers). Compose files live in [`deploy/`](../../../deploy/). For Modal itself, see [Modal + vLLM](modal-vllm.md). For a laptop Python install without Docker, see [Deployment](./).

`MAILROOM_API_TOKEN` is **required** on every compose `app` service. The container binds `0.0.0.0`; audit L-2 refuses an off-loopback bind without a live bearer token.

## Compose files

| Path                                                                                                                           | Contents                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| [`deploy/docker-compose.yml`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.yml)                     | Base file — `app` + optional `watcher`. Modes A / B / mixed / M share it         |
| [`deploy/docker-compose.ollama.yml`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.ollama.yml)       | Mode A overlay: Ollama sidecar                                                   |
| [`deploy/docker-compose.llamafile.yml`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.llamafile.yml) | Mode A overlay: llamafile sidecar                                                |
| [`deploy/docker-compose.full.yml`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.full.yml)           | Mode G — app, ops-monitor, watchdog, postgres, LiteLLM gateway, optional Phoenix |
| [`deploy/docker-compose.producer.yml`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.producer.yml)   | The-Mailroom REVIEW pairing (reachable producer)                                 |
| [`deploy/llamafile/`](../../../deploy/llamafile/)                                                                              | llamafile sidecar image (pinned binary) + entrypoint                             |
| [`deploy/models/`](../../../deploy/models/)                                                                                    | Host staging for GGUF / ModernBERT weights — never committed                     |

The root [`Dockerfile`](https://github.com/Exios66/llm-mailroom/blob/main/Dockerfile) is a multi-stage build (builder → model → runtime). It runs as non-root (`mailroom`, uid 10001) and ships a `HEALTHCHECK` against `/health`. Compose sets `security_opt: no-new-privileges` and `user: 10001:10001`.

## Deployment matrix

One image, several LLM postures, selected by env plus an optional overlay:

| Mode                    | LLM source            | Provider env                                                                 | BERT intake                   | Invocation                                                                       |
| ----------------------- | --------------------- | ---------------------------------------------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------- |
| **B** — API             | OpenRouter            | `DEFAULT_PROVIDER=openrouter` + `OPENROUTER_API_KEY`                         | off                           | `docker compose -f deploy/docker-compose.yml --env-file .env up -d --build`      |
| **Mixed** (dev default) | OpenRouter            | as B                                                                         | on (`MAILROOM_BERT_INTAKE=1`) | same base file                                                                   |
| **A-ollama**            | Ollama sidecar        | `DEFAULT_PROVIDER=ollama` + `OLLAMA_BASE_URL=http://ollama:11434/v1`         | on                            | base + `-f deploy/docker-compose.ollama.yml`                                     |
| **A-llamafile**         | llamafile sidecar     | `DEFAULT_PROVIDER=llamafile` + `LLAMAFILE_BASE_URL=http://llamafile:8080/v1` | on                            | base + `-f deploy/docker-compose.llamafile.yml`                                  |
| **M** — Modal vLLM      | Modal app             | `DEFAULT_PROVIDER=vllm` + `VLLM_BASE_URL=<modal.run>/v1` + `VLLM_API_KEY`    | free                          | [Modal + vLLM](modal-vllm.md) then the base file                                 |
| **G** — full stack      | LiteLLM → Modal tiers | `DEFAULT_PROVIDER=litellm`                                                   | optional                      | `docker compose -f deploy/docker-compose.full.yml --env-file .env up -d --build` |

The API embeds the inbox watcher (`MAILROOM_EMBED_WATCHER=1`). A `watcher` service exists for watcher-only runs with `MAILROOM_EMBED_WATCHER=0` (`watcher.lock` is an flock — exactly one intake authority per `/data`). Overlay runs must pass `--profile local-llm` so the sidecar's `profiles:` gate matches its `depends_on` (Compose validates against **active** profiles).

Secrets come from the host `.env` via `--env-file`. Never bake tokens into images.

## Mode B / Mixed (OpenRouter)

```bash
cp .env.example .env   # set MAILROOM_API_TOKEN and OPENROUTER_API_KEY
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

Mixed (BERT intake on the same image): set `MAILROOM_BERT_INTAKE=1` in `.env`. The default build arg `MAILROOM_PIP_EXTRAS=bert` installs onnxruntime + tokenizers into the image.

Lean Mode B (no BERT extra):

```bash
MAILROOM_PIP_EXTRAS= docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

The API listens on `:8000`. Try-it-out: `http://127.0.0.1:8000/docs`. Health: `curl -sS http://127.0.0.1:8000/health`.

## Mode A — Ollama (offline)

```bash
# DEFAULT_PROVIDER=ollama
# OLLAMA_BASE_URL=http://ollama:11434/v1
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ollama.yml \
  --env-file .env up -d --build
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ollama.yml \
  exec ollama ollama pull qwen3:7b        # once; needs network
```

Host-side smoke against the sidecar: `curl http://localhost:11434/v1/models`. Agent model ids still come from `taxonomy.yaml` — run `PYTHONPATH=src python src/scripts/cutover.py --list` and pull matching names. Cutover details: [Local models](../local-models.md).

## Mode A — llamafile (offline)

Prefer llamafile over Ollama when you want a zero-daemon, single-binary GGUF server pinned to an exact release. The sidecar image bakes the pinned binary (`deploy/llamafile/Dockerfile`, `LLAMAFILE_VERSION`, default `0.10.4`). GGUF weights are bind-mounted from `deploy/models/llamafile/` (`:ro`) — see [`deploy/models/README.md`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/models/README.md).

```bash
# DEFAULT_PROVIDER=llamafile
# LLAMAFILE_BASE_URL=http://llamafile:8080/v1
# LLAMAFILE_ALIAS must match taxonomy.yaml llamafile_model_map:
LLAMAFILE_MODEL=/models/llamafile/qwen3-7b-instruct-q4_k_m.gguf \
docker compose --profile local-llm \
  -f deploy/docker-compose.yml -f deploy/docker-compose.llamafile.yml \
  --env-file .env up -d --build
```

Serve flags (entrypoint): `--server`, `--jinja`, `--ctx-size`, `--mlock`, `-np 1`, `--gpu` (default `disable` = CPU; `auto|nvidia|amd|apple|vulkan` on GPU hosts). Healthcheck: `GET /health` (llama.cpp JSON `{"status":"ok"}`).

Resource notes (CPU, no GPU): Qwen3 **7B Q4\_K\_M ≈ 4.7 GB** weights (≈ 6–8 GB RSS with a 16k-token KV cache), **14B ≈ 8.5 GB** (≈ 11–13 GB RSS). A 7B-class model is the practical ceiling on a 16 GB laptop; 27B-class only on GPU hosts. The model loads at start and stays resident (`--mlock`) — there is no unload-on-idle like Ollama's `KEEP_ALIVE`.

## Mode G — full stack (LiteLLM + Modal GPU tiers)

[`deploy/docker-compose.full.yml`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/docker-compose.full.yml) is the single-host production topology: `app` (API + embedded watcher + LangGraph), `ops-monitor`, `watchdog` (shares the app's PID namespace), `postgres`, `llm-gateway` (LiteLLM), and optional `phoenix` (`--profile phoenix`).

Startup is healthcheck-gated: postgres + gateway healthy → app healthy (schema created, `/health` green) → ops-monitor + watchdog.

Only LLM calls leave the host. Documents move through the filesystem bins on the shared `mailroom_data` volume (`inbox` → `processing` → `archive` / `review` / `failed`). The gateway runs `num_retries: 0` and **no trace callbacks**. The mailroom retry ladder (a gateway 503 is a Modal cold start → long backoff) and client-side tracing (Langfuse / Phoenix / Braintrust) stay authoritative.

### 1. Deploy the GPU tiers on Modal

```bash
pip install -e ".[deploy]"
modal token new
modal deploy deploy/modal_vllm.py
```

That prints one URL per tier. Defaults:

| Tier    | Alias              | Default model                   | GPU  | Context | Agents                                                     |
| ------- | ------------------ | ------------------------------- | ---- | ------- | ---------------------------------------------------------- |
| fast    | `mailroom-fast`    | Qwen3-8B-FP8                    | L4   | 32k     | sorter, sorter\_reviewer, intake, gmail\_triage, relations |
| extract | `mailroom-extract` | Qwen3-30B-A3B-Instruct-2507-FP8 | L40S | 64k     | specialists, arbiter, boss, judge                          |
| vision  | `mailroom-vision`  | Qwen3-VL-8B-Instruct-FP8        | L4   | 32k     | pdf\_transcriber, image\_extractor                         |

Per-tier knobs: `MODAL_VLLM_<TIER>_<KNOB>` (`MODEL`, `GPU`, `MAX_MODEL_LEN`, `MAX_NUM_SEQS`, `MAX_INPUTS`, `MAX_CONTAINERS`, `MIN_CONTAINERS`, `SCALEDOWN_SECONDS`, …). Unscoped `MODAL_VLLM_<KNOB>` still applies to `fast`. `MODAL_VLLM_TIERS` selects which tiers deploy. Full knob list: [Modal + vLLM](modal-vllm.md).

### 2. Fill `.env`

From [`.env.example`](https://github.com/Exios66/llm-mailroom/blob/main/.env.example):

| Variable                 | Role                                                                     |
| ------------------------ | ------------------------------------------------------------------------ |
| `MAILROOM_API_TOKEN`     | Bearer for the API (`0.0.0.0` bind)                                      |
| `POSTGRES_PASSWORD`      | Required — the Postgres image will not initialise without it             |
| `LITELLM_MASTER_KEY`     | Gateway auth; the app sends it as `LITELLM_API_KEY`                      |
| `LITELLM_IMAGE`          | Default `ghcr.io/berriai/litellm:v1.104.0`. Do **not** use `main-stable` |
| `MODAL_FAST_URL`         | `https://<ws>--mailroom-vllm-fast.modal.run/v1`                          |
| `MODAL_EXTRACT_URL`      | `https://<ws>--mailroom-vllm-extract.modal.run/v1`                       |
| `MODAL_VISION_URL`       | `https://<ws>--mailroom-vllm-vision.modal.run/v1`                        |
| `MODAL_VLLM_API_TOKEN`   | Bearer the Modal tiers were deployed with                                |
| `MAILROOM_GATEWAY_TIERS` | Optional runtime override, e.g. `sorter=api,boss=fast`                   |
| `OPENROUTER_API_KEY`     | Only if some agent is on the `api` tier                                  |
| `LANGFUSE_*`             | Langfuse Cloud tracing (or use `--profile phoenix`)                      |

Routing lives in `src/config/taxonomy.yaml` (`gateway:` + per-agent `tier:`). `MAILROOM_GATEWAY_TIERS=sorter=api` moves one agent to OpenRouter at runtime.

### 3. Bring the host stack up

```bash
docker compose -f deploy/docker-compose.full.yml --env-file .env up -d --build
# optional local traces:
docker compose -f deploy/docker-compose.full.yml --env-file .env --profile phoenix up -d --build
```

### 4. Smoke

```bash
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --check
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --warm
PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --run
```

`--check` is network-free (taxonomy tiers, LiteLLM aliases, and Modal tiers agree). `--warm` warms every tier. `--run` sends one document per class end to end and checks each node's Langfuse generation model against its expected tier.

## Producer image (The-Mailroom pairing)

The-Mailroom Observatory needs a **reachable** llm-mailroom producer for the floor lamp, Inbox **Queue a document** (`POST /v1/upload`), and REVIEW resolve (`MAILROOM_PIPELINE_URL` + `MAILROOM_PIPELINE_TOKEN` + `MAILROOM_PIPELINE_API_PREFIX=/v1`). Pairing notes: [`deploy/space/PAIRING.md`](https://github.com/Exios66/llm-mailroom/blob/main/deploy/space/PAIRING.md). Railway and Hugging Face Space recipes stay on [Deployment](./).

```bash
docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build
```

On The-Mailroom:

```bash
MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
MAILROOM_PIPELINE_API_PREFIX=/v1
```

`PYTHONPATH=src python src/scripts/publish_space.py --check` asserts the Dockerfile baseline before publishing a Hub Space.

## Image build args (BERT fast path)

The root Dockerfile is ARGuable per mode:

| ARG                 | Default                                             | Purpose                                                                                                    |
| ------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `PIP_EXTRAS`        | `bert`                                              | Optional extras (`""` = leanest Mode B image; `[bert]` = onnxruntime + tokenizers + huggingface-hub)       |
| `ML_EXPORT_ONNX`    | `1`                                                 | Hook for the future int8 ONNX export pipeline                                                              |
| `ML_MODEL_REPO`     | `Lucius-Morningstar/mailroom-modernbert-classifier` | Bundle source                                                                                              |
| `ML_MODEL_REVISION` | `main`                                              | Pin a Hub commit SHA for reproducible builds                                                               |
| `ML_BUILD_NONE`     | `0`                                                 | `1` = skip the model download (empty bundle dir; BERT lane fails open)                                     |
| `FINAL_USER`        | `mailroom`                                          | Docker/Compose contract (uid 10001). **Modal builds must pass `root`** — SDK 1.5.5 has no `container_user` |

Runtime: `ML_MODEL_DIR` (default `/models/mailroom-modernbert-classifier`) + `MAILROOM_BERT_INTAKE=0/1` (off by default). The BERT lane in `agents/bert_intake.py` fails **open** — missing package, bundle, or model degrades to the deterministic clerk and never crashes intake. The `[bert]` extra contains **no torch**: runtime inference is onnxruntime CPU only.

## Related

* [Deployment](./) — laptop install, Railway, backup
* [Modal + vLLM](modal-vllm.md) — GPU serve and cutover
* [Local models](../local-models.md) — Ollama / vLLM provider cutover
* [Configuration](../configuration.md) — env vars
* [API](../api.md) — HTTP contract the compose `app` exposes
