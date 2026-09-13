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
    MODAL_VLLM_MAX_MODEL_LEN   int tokens            (default 16384; 32768 only for AWQ/FP8 rows)
    MODAL_VLLM_GPU_MEMORY_UTILIZATION  0.0–1.0      (default 0.90)
    MODAL_VLLM_MAX_NUM_SEQS    int                   (default 256)
    MODAL_VLLM_TP_SIZE         int                   (default: from the GPU `:N` suffix)
    MODAL_VLLM_SCALEDOWN_SECONDS       int          (default 900; idle before scale-to-zero)
    MODAL_VLLM_STARTUP_TIMEOUT_SECONDS int          (default 1200; first-boot ceiling)
    MODAL_VLLM_REVISION        Hub revision SHA      (optional; pins weights)
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
MAX_MODEL_LEN = os.environ.get("MODAL_VLLM_MAX_MODEL_LEN", "16384")
REVISION = os.environ.get("MODAL_VLLM_REVISION", "")

# Memory budget for a test deploy: vLLM's default is 0.92 of the GPU; 0.90
# keeps headroom on the 24 GB L4 (and on shared local GPUs) at a negligible
# KV-pool cost. Raise deliberately for throughput runs.
GPU_MEMORY_UTILIZATION = os.environ.get("MODAL_VLLM_GPU_MEMORY_UTILIZATION", "0.90")
# vLLM resolves 256 for the OpenAI server on <=70 GB GPUs; pinned here so
# local compose and Modal schedule the same concurrency on any GPU class.
MAX_NUM_SEQS = os.environ.get("MODAL_VLLM_MAX_NUM_SEQS", "256")

# Tensor-parallel size: 1 (single GPU) by default. For a multi-GPU container
# (e.g. MODAL_VLLM_GPU="A100-80GB:2" for 70B-class) this MUST match the `:N`
# suffix or vLLM silently serves on 1 GPU and OOMs. Derive the default from
# the GPU knob suffix; override explicitly when needed (DMR-051, sandbox
# sibling contract).
TP_SIZE = os.environ.get("MODAL_VLLM_TP_SIZE", "") or str(
    int(os.environ.get("MODAL_VLLM_GPU", "L4").split(":")[1])
    if ":" in os.environ.get("MODAL_VLLM_GPU", "L4")
    else 1
)

# Cost/scale knobs (sandbox sibling contract): scale-to-zero idle window and
# the long first-boot ceiling (weight download + CUDA-graph build).
SCALEDOWN_SECONDS = int(os.environ.get("MODAL_VLLM_SCALEDOWN_SECONDS", 15 * 60))
STARTUP_TIMEOUT_SECONDS = int(
    os.environ.get("MODAL_VLLM_STARTUP_TIMEOUT_SECONDS", 20 * 60)
)

# Pinned for reproducible deploys; bump deliberately (driver/CUDA compat).
VLLM_IMAGE_TAG = os.environ.get("MODAL_VLLM_IMAGE_TAG", "v0.28.0")

CONFIG_ENV_KEYS = (
    "MODAL_VLLM_MODEL",
    "MODAL_VLLM_QUANTIZATION",
    "MODAL_VLLM_MAX_MODEL_LEN",
    "MODAL_VLLM_GPU_MEMORY_UTILIZATION",
    "MODAL_VLLM_MAX_NUM_SEQS",
    "MODAL_VLLM_TP_SIZE",
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
    # DMR-056: huggingface_hub 1.x has no [hf_transfer] extra (pip warns and
    # succeeds); Xet is the default transfer backend in 1.x, so the plain
    # package + HF_XET_HIGH_PERFORMANCE is the correct, warning-free setup.
    .run_commands("pip install --no-cache-dir huggingface_hub")
    .env(
        {
            "HF_XET_HIGH_PERFORMANCE": "1",
        }
    )
)

# Slim image for the pre-warm function (no GPU, no vLLM).
download_image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("huggingface_hub")
    .env(
        {
            "HF_XET_HIGH_PERFORMANCE": "1",
        }
    )
)

# DMR-056: cost-allocation tags — parity with the sandbox sibling app.
app = modal.App(
    APP_NAME,
    image=image,
    tags={
        "project": "digital-mailroom",
        "package": "llm-mailroom",
        "purpose": "remote-gpu-testing",
    },
)


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
        "--gpu-memory-utilization",
        GPU_MEMORY_UTILIZATION,
        "--max-num-seqs",
        MAX_NUM_SEQS,
    ]
    if REVISION:
        # Pin the Hub revision to avoid silent weight changes.
        cmd += ["--revision", REVISION]
    if QUANTIZATION:
        cmd += ["--quantization", QUANTIZATION]
    if TP_SIZE and TP_SIZE != "1":
        # Multi-GPU containers must pass this or vLLM uses only 1 GPU and OOMs.
        cmd += ["--tensor-parallel-size", TP_SIZE]
    # Legal-document workloads are bursty and latency-tolerant: batch freely.
    cmd += ["--no-enable-log-requests"]
    return cmd


def _masked_config() -> dict[str, str]:
    """The effective serve config for boot diagnostics — secrets masked."""

    def presence(name: str) -> str:
        return "set" if os.environ.get(name, "").strip() else "unset"

    return {
        "model": os.environ.get("MODAL_VLLM_MODEL", MODEL),
        "gpu": GPU,
        "image": VLLM_IMAGE_TAG,
        "max_model_len": MAX_MODEL_LEN,
        "gpu_memory_utilization": GPU_MEMORY_UTILIZATION,
        "max_num_seqs": MAX_NUM_SEQS,
        "tensor_parallel_size": TP_SIZE,
        "quantization": QUANTIZATION or "unset(bf16)",
        "revision": REVISION or "unset(tip)",
        "VLLM_API_KEY": presence("MODAL_VLLM_API_TOKEN"),
        "HF_TOKEN": presence("HF_TOKEN"),
        "scaledown_seconds": str(SCALEDOWN_SECONDS),
        "startup_timeout_seconds": str(STARTUP_TIMEOUT_SECONDS),
    }


@app.function(
    gpu=GPU,
    volumes={HF_CACHE_MOUNT: hf_cache, VLLM_CACHE_MOUNT: vllm_cache},
    secrets=_config_secrets(),
    timeout=60 * 30,
    scaledown_window=SCALEDOWN_SECONDS,
    startup_timeout=STARTUP_TIMEOUT_SECONDS,
    # Long warm-up (weight download on first cold boot) before health checks.
)
@modal.web_server(port=SERVER_PORT, startup_timeout=STARTUP_TIMEOUT_SECONDS)
def serve() -> None:
    model = os.environ.get("MODAL_VLLM_MODEL", MODEL)
    cmd = build_vllm_command(model)
    # Boot diagnostics (masked): the container log shows the EFFECTIVE config
    # so a failed cold start is diagnosable without re-deriving env (DMR-053).
    print("=== mailroom-vllm serve config (masked) ===")
    for key, value in _masked_config().items():
        print(f"  {key}: {value}")
    print("starting:", " ".join(cmd))  # never contains secret values
    subprocess.Popen(cmd, env={**os.environ, **_server_env()})


@app.function(
    image=download_image,
    volumes={HF_CACHE_MOUNT: hf_cache},
    secrets=_config_secrets(),
    timeout=60 * 45,
)
def download_model(model: str = "", revision: str = "") -> None:
    """Pre-warm the HF cache Volume so the first `serve` boot skips downloads.

    ``modal run deploy/modal_vllm.py::download_model [--model ...]``

    Fails loudly when nothing was cached (DMR-053): a silent empty snapshot
    would leave the first serve boot downloading weights anyway.
    """
    from huggingface_hub import snapshot_download

    model = model or os.environ.get("MODAL_VLLM_MODEL", MODEL)
    revision = revision or os.environ.get("MODAL_VLLM_REVISION", REVISION) or None
    print(f"pre-warming {model}" + (f"@{revision}" if revision else ""))
    # DMR-056: huggingface_hub 1.x returns the snapshot DIRECTORY (str), not a
    # file list — the old isinstance(paths, list) guard could never fire, so
    # an empty snapshot would have been silently "cached". Count the files in
    # the returned dir and raise loudly on an empty result.
    snapshot_dir = snapshot_download(repo_id=model, revision=revision)
    n_files = sum(1 for _ in Path(snapshot_dir).rglob("*")) if snapshot_dir else 0
    if not n_files:
        raise SystemExit(
            f"snapshot_download returned an empty snapshot for {model} — check the "
            "repo id, the revision, and HF_TOKEN for gated repos"
        )
    hf_cache.commit()
    print(f"cached {model}" + (f"@{revision}" if revision else "") + f" ({n_files} file(s))")


@app.local_entrypoint()
def main(check: bool = False, debug: bool = False) -> None:
    """`modal run modal_vllm.py [--check] [--debug]` — guidance + probe (DMR-056)."""
    name = Path(__file__).name
    base = os.environ.get("VLLM_BASE_URL", "").rstrip("/")
    print(f"Deploy with:  modal deploy {name}")
    print(f"Serving model: {os.environ.get('MODAL_VLLM_MODEL', MODEL)} on GPU {GPU}")
    print(f"Image: vllm/vllm-openai:{VLLM_IMAGE_TAG}")
    print(f"Endpoint: {base or 'set VLLM_BASE_URL after deploy'}")
    if debug:
        print("=== resolved config (masked) ===")
        for key, value in _masked_config().items():
            print(f"  {key}: {value}")
    if check:
        _smoke_check(base)
    print(
        "Then point mailroom at it:\n"
        "  DEFAULT_PROVIDER=vllm\n"
        "  VLLM_BASE_URL=https://<workspace>--mailroom-vllm-serve.modal.run/v1\n"
        "  VLLM_API_KEY=<same value as MODAL_VLLM_API_TOKEN>"
    )


def _smoke_check(base: str) -> None:
    """Bearer-aware `/models` probe for `modal run ... --check` (DMR-053)."""
    import httpx

    if not base:
        raise SystemExit(
            "VLLM_BASE_URL is not set — export the URL printed by `modal deploy` "
            "(https://<workspace>--mailroom-vllm-serve.modal.run/v1)"
        )
    token = os.environ.get("VLLM_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.get(f"{base}/models", headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"probe failed: {type(exc).__name__}: {exc}\n"
            "hints: is the app deployed (`modal app list`)? is VLLM_BASE_URL the "
            "modal.run URL, not localhost?"
        ) from exc
    if resp.status_code == 401:
        raise SystemExit(
            f"401 from {base}/models — the server enforces a bearer token; set "
            "VLLM_API_KEY to the deployed MODAL_VLLM_API_TOKEN value"
        )
    if resp.status_code >= 400:
        body = resp.text[:400]
        raise SystemExit(
            f"HTTP {resp.status_code} from {base}/models\nresponse body: {body!r}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SystemExit(
            f"{base}/models returned non-JSON (HTTP {resp.status_code}): "
            f"{resp.text[:400]!r}"
        ) from exc
    ids = [
        item.get("id")
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    print(f"ok: {base}/models -> {ids}")
    if ids and os.environ.get("MODAL_VLLM_MODEL", MODEL) not in ids:
        print(
            f"note: served model(s) {ids} differ from MODAL_VLLM_MODEL="
            f"{os.environ.get('MODAL_VLLM_MODEL', MODEL)} — the provider may 404 "
            "on the configured model id"
        )
