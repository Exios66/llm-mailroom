"""KANBAN-064 — Modal-deployed vLLM server for llm-mailroom (offline capability).

A configuration CAPABILITY, not a serving-path change: OpenRouter stays the
primary LLM backend unless someone explicitly sets ``DEFAULT_PROVIDER=vllm``
in the mailroom ``.env`` and points ``VLLM_BASE_URL`` at this deployment's
URL. See deploy/README.md for the full flip-the-switch instructions.

Deploy:

    cd llm-mailroom/deploy
    pip install modal          # once
    modal token new            # once
    modal deploy modal_vllm.py # prints the https://...modal.run URL

Dev smoke test without deploying (temporary URL while the command runs):

    modal serve modal_vllm.py

All knobs come from environment variables at DEPLOY time (baked into the
app via modal.Secret.from_dict), so no code edits are needed to change
model, GPU size, or quantization:

    MODAL_VLLM_MODEL           HF repo id            (default Qwen/Qwen3-8B)
    MODAL_VLLM_GPU             Modal GPU string      (default L4)
    MODAL_VLLM_QUANTIZATION    awq | gptq | ...      (default: unset = fp16/bf16)
    MODAL_VLLM_MAX_MODEL_LEN   int tokens            (default 32768)
    MODAL_VLLM_API_TOKEN      bearer token the server REQUIRES (recommended;
                               leave unset only for throwaway experiments)
    HF_TOKEN                   for gated/private repos (optional)

The served API is OpenAI-compatible (/v1/chat/completions, /v1/models,
/v1/completions), which is exactly the interface mailroom's ``vllm``
provider already speaks through ``get_llm`` -> ``resolve_provider``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import modal

APP_NAME = "mailroom-vllm"
SERVER_PORT = 8000
HF_CACHE_VOLUME_NAME = "mailroom-hf-cache"
VLLM_CACHE_VOLUME_NAME = "mailroom-vllm-cache"
HF_CACHE_MOUNT = "/root/.cache/huggingface"
VLLM_CACHE_MOUNT = "/root/.cache/vllm"

MODEL = os.environ.get("MODAL_VLLM_MODEL", "Qwen/Qwen3-8B")
GPU = os.environ.get("MODAL_VLLM_GPU", "L4")
QUANTIZATION = os.environ.get("MODAL_VLLM_QUANTIZATION", "")
MAX_MODEL_LEN = os.environ.get("MODAL_VLLM_MAX_MODEL_LEN", "32768")
REVISION = os.environ.get("MODAL_VLLM_REVISION", "")

# Memory budget for a test deploy: vLLM's default is 0.92 of the GPU; 0.90
# keeps headroom on the 24 GB L4 (and on shared local GPUs) at a negligible
# KV-pool cost. Raise deliberately for throughput runs.
GPU_MEMORY_UTILIZATION = os.environ.get("MODAL_VLLM_GPU_MEMORY_UTILIZATION", "0.90")
# vLLM resolves 256 for the OpenAI server on <=70 GB GPUs; pinned here so
# local compose and Modal schedule the same concurrency on any GPU class.
MAX_NUM_SEQS = os.environ.get("MODAL_VLLM_MAX_NUM_SEQS", "256")

# Pinned for reproducible deploys; bump deliberately (driver/CUDA compat).
VLLM_IMAGE_TAG = os.environ.get("MODAL_VLLM_IMAGE_TAG", "v0.28.0")

CONFIG_ENV_KEYS = (
    "MODAL_VLLM_MODEL",
    "MODAL_VLLM_QUANTIZATION",
    "MODAL_VLLM_MAX_MODEL_LEN",
    "MODAL_VLLM_GPU_MEMORY_UTILIZATION",
    "MODAL_VLLM_MAX_NUM_SEQS",
    "MODAL_VLLM_REVISION",
    "MODAL_VLLM_API_TOKEN",
    "HF_TOKEN",
)


def _config_secrets() -> list[modal.Secret]:
    """Deploy-time knobs as an inline Secret (empty list when none are set).

    SDK 1.5.5 removed ``Secret.from_local``. Its replacement,
    ``Secret.from_local_environ``, raises when any named variable is missing,
    but ``HF_TOKEN`` and ``MODAL_VLLM_API_TOKEN`` are optional — so build the
    dict ourselves and let ``Secret.from_dict`` skip absent keys.
    """
    values = {
        name: os.environ.get(name) for name in CONFIG_ENV_KEYS if os.environ.get(name)
    }
    if not values:
        return []
    return [modal.Secret.from_dict(values)]

hf_cache = modal.Volume.from_name(HF_CACHE_VOLUME_NAME, create_if_missing=True)
# vLLM JIT/CUDA-graph compile artifacts: caching them cuts recompilation on
# cold start from minutes to ~seconds (Modal vLLM example, 2026-09).
vllm_cache = modal.Volume.from_name(VLLM_CACHE_VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.from_registry(f"vllm/vllm-openai:{VLLM_IMAGE_TAG}", add_python="3.12")
    .run_commands("pip install --no-cache-dir huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

app = modal.App(APP_NAME, image=image)


def _server_env() -> dict[str, str]:
    """Environment for the vLLM process inside the container."""
    env: dict[str, str] = {}
    api_token = os.environ.get("MODAL_VLLM_API_TOKEN", "").strip()
    if api_token:
        # vLLM's native bearer enforcement: requests without
        # `Authorization: Bearer <token>` get a 401.
        env["VLLM_API_KEY"] = api_token
    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if hf_token:
        env["HF_TOKEN"] = hf_token
    return env


def build_vllm_command(model: str) -> list[str]:
    """Assemble the `vllm serve` argv. Kept pure for unit testing."""
    cmd = [
        "vllm",
        "serve",
        model,
        "--host",
        "0.0.0.0",
        "--port",
        str(SERVER_PORT),
        "--max-model-len",
        MAX_MODEL_LEN,
    ]
    if QUANTIZATION:
        cmd += ["--quantization", QUANTIZATION]
    # Legal-document workloads are bursty and latency-tolerant: batch freely.
    cmd += ["--no-enable-log-requests"]
    return cmd


@app.function(
    gpu=GPU,
    volumes={"/root/.cache/huggingface": hf_cache},
    secrets=_config_secrets(),
    timeout=60 * 30,
    scaledown_window=15 * 60,
    # Long warm-up (weight download on first cold boot) before health checks.
)
@modal.web_server(port=SERVER_PORT, startup_timeout=60 * 20)
def serve() -> None:
    model = os.environ.get("MODAL_VLLM_MODEL", MODEL)
    cmd = build_vllm_command(model)
    print("starting:", " ".join(cmd))
    subprocess.Popen(cmd, env={**os.environ, **_server_env()})


@app.local_entrypoint()
def main() -> None:
    """`modal run modal_vllm.py` prints deployment guidance without serving."""
    print(f"Deploy with:  modal deploy {Path(__file__).name}")
    print(f"Serving model: {os.environ.get('MODAL_VLLM_MODEL', MODEL)} on GPU {GPU}")
    print(
        "Then point mailroom at it:\n"
        "  DEFAULT_PROVIDER=vllm\n"
        f"  VLLM_BASE_URL=https://modal.com>--{APP_NAME}-serve.modal.run/v1\n"
        "  VLLM_API_KEY=<same value as MODAL_VLLM_API_TOKEN>"
    )
