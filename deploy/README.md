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
