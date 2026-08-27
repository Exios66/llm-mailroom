---
name: modal
description: Deploy and cut over to Modal-hosted vLLM for llm-mailroom (mailroom-vllm app). Use when the user needs remote GPU inference, DEFAULT_PROVIDER=vllm, modal deploy/token, or MODAL_VLLM_* knobs — not for default OpenRouter or CPU/Ollama work.
---

# Modal vLLM (remote GPU serve)

**When:** No suitable local GPU, user asks for Modal, or
`DEFAULT_PROVIDER=vllm` against a `*.modal.run` URL.  
**Prefer OpenRouter** for everyday production ([openrouter](../openrouter/SKILL.md)).
Prefer [ollama](../ollama/SKILL.md) for local CPU smoke.

## Deploy

```bash
pip install -e ".[deploy]"
modal token new
export MODAL_VLLM_MODEL=Qwen/Qwen3-8B
export MODAL_VLLM_GPU=L4
export MODAL_VLLM_API_TOKEN="$(openssl rand -hex 24)"
cd deploy && modal deploy modal_vllm.py
```

App name: **`mailroom-vllm`** (not the sandbox app `sandbox-vllm`). Source:
`deploy/modal_vllm.py`. Smoke without deploy: `modal serve modal_vllm.py`.

Tear down: `modal app stop mailroom-vllm`.

## Cut over the pipeline

```bash
# .env
DEFAULT_PROVIDER=vllm
VLLM_BASE_URL=https://<workspace>--mailroom-vllm-serve.modal.run/v1
VLLM_API_KEY=<same as MODAL_VLLM_API_TOKEN>
```

Every agent still goes through `get_llm()`. Flip back with
`DEFAULT_PROVIDER=openrouter`.

## Knobs (deploy-time)

| Env | Default |
| --- | --- |
| `MODAL_VLLM_MODEL` | `Qwen/Qwen3-8B` |
| `MODAL_VLLM_GPU` | `L4` |
| `MODAL_VLLM_MAX_MODEL_LEN` | `32768` |
| `MODAL_VLLM_QUANTIZATION` | empty |
| `HF_TOKEN` | optional Hub auth for gated weights |

## Boundaries

- Do not use Modal for pytest or `--mock`.
- Do not rename the app to the sandbox name.
- Sibling `llm-entity-extraction` can share the same server via its
  `OPENROUTER_BASE_URL` seam.

## Related

- Production default: [openrouter](../openrouter/SKILL.md)
- Hub weights: [huggingface](../huggingface/SKILL.md)
- Docs: `deploy/README.md`
