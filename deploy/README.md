# Modal-deployed vLLM — offline serving capability (KANBAN-064)

**Status: framework-in-place. OpenRouter remains llm-mailroom's primary LLM
path.** This is the pre-built escape hatch: when you want the pipeline
running against a self-hosted model (offline mode, cost control, or data
egress concerns), the entire switch is three `.env` lines.

The deployment serves vLLM's **OpenAI-compatible API** (`/v1/chat/completions`,
`/v1/models`) on a Modal GPU. Mailroom's existing `vllm` provider
(`llm/providers.py`) already speaks exactly that protocol through the same
`get_llm()` door as every other provider — no agent or graph code knows or
cares where its LLM lives.

## 1. One-time setup

```bash
pip install -e ".[deploy]"     # modal CLI (deploy-time only, not runtime)
modal token new                # authenticate this machine
```

## 2. Configure + deploy

All knobs are environment variables read at deploy time and baked into the
app via `modal.Secret.from_local` — change behavior without touching code:

| Variable | Default | Purpose |
|---|---|---|
| `MODAL_VLLM_MODEL` | `Qwen/Qwen3-8B` | HF repo id to serve |
| `MODAL_VLLM_GPU` | `L4` | Modal GPU (`L4`, `A10G`, `A100-40GB`, `A100-80GB`, `H100`, …) |
| `MODAL_VLLM_QUANTIZATION` | *(unset)* | e.g. `awq`, `gptq` — halves VRAM, small quality cost |
| `MODAL_VLLM_MAX_MODEL_LEN` | `32768` | context window (tokens) |
| `MODAL_VLLM_API_TOKEN` | *(unset)* | **bearer token the server REQUIRES** — set one for anything non-throwaway |
| `HF_TOKEN` | *(unset)* | only for gated/private model repos |

```bash
export MODAL_VLLM_MODEL="Qwen/Qwen3-8B"
export MODAL_VLLM_GPU="L4"
export MODAL_VLLM_API_TOKEN="$(openssl rand -hex 24)"
cd deploy
modal deploy modal_vllm.py     # prints https://<workspace>--mailroom-vllm-serve.modal.run
```

Quick smoke test without deploying (temporary URL, dies when Ctrl-C):

```bash
modal serve modal_vllm.py
```

## 3. Flip mailroom onto it

In `.env` (copy `.env.example` if starting fresh):

```bash
DEFAULT_PROVIDER=vllm
VLLM_BASE_URL=https://<workspace>--mailroom-vllm-serve.modal.run/v1
VLLM_API_KEY=<same value as MODAL_VLLM_API_TOKEN>
```

Then run the pipeline normally — watcher, API, pilots, everything. Every
agent (sorter, specialists, judge, reviewer, arbiter) resolves through
`get_llm()` and lands on the local server. **Flip back** by restoring
`DEFAULT_PROVIDER=openrouter`; nothing else remembers the local mode.

Keyless local mode also works: run `vllm serve` on your own GPU box (or
`MODAL_VLLM_API_TOKEN` unset) and leave `VLLM_API_KEY` empty — the client
sends `api_key="not-needed"`.

## 4. Model / GPU selection notes

- The mailroom prompt stack was tuned on Qwen-family models (OpenRouter
  `qwen/qwen3.7-flash` is the production champion) — `Qwen/Qwen3-8B` or
  `Qwen/Qwen3-14B` are the closest local matches. Bigger = better JSON
  discipline on the extraction specialists.
- `L4` (24 GB) runs 8B-class fp16 comfortably; 14B-class wants `A10G` or
  AWQ quantization on `L4`. 32B-class needs `A100-40GB`+.
- Legal documents are long: keep `MODAL_VLLM_MAX_MODEL_LEN` ≥ 16384 or the
  chunked extraction prompts will truncate.

## 5. Cost + teardown

Modal bills per GPU-second while the container is up (plus a scaledown
window of 15 min after the last request). For a fire-and-drill setup:

```bash
modal app stop mailroom-vllm   # stop billing immediately
```

The HF weights cache volume (`mailroom-hf-cache`) persists across cold
boots, so restarts skip the multi-GB download.

## 6. Troubleshooting

- **401 on requests** → `VLLM_API_KEY` in `.env` must equal the
  `MODAL_VLLM_API_TOKEN` the app was deployed with. Redeploy if you
  rotated the token.
- **Cold start takes minutes** → first boot downloads weights to the
  volume; subsequent boots are warm. `modal serve` is best for iteration,
  `modal deploy` for standing capability.
- **CUDA OOM** → lower `MODAL_VLLM_MAX_MODEL_LEN`, set
  `MODAL_VLLM_QUANTIZATION=awq`, or bump `MODAL_VLLM_GPU`.
- **Model 404s at boot** → gated repo: export `HF_TOKEN` before deploy.
- **`modal: command not found`** → `pip install -e ".[deploy]"` in the
  mailroom venv you're invoking from.
