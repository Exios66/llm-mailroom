"""Modal-deployed vLLM GPU tiers for llm-mailroom (KANBAN-064, multi-tier).

One Modal app (``mailroom-vllm``) with one web function per MODEL TIER —
agents are grouped by the model they need, never one GPU per graph node:

    tier      default model                               GPU   context  agents (taxonomy `tier:`)
    fast      Qwen/Qwen3-8B-FP8                           L4    32768    sorter, sorter_reviewer, intake, gmail_triage, relations
    extract   Qwen/Qwen3-30B-A3B-Instruct-2507-FP8        L40S  65536    5 specialists, arbiter, judge, boss
    vision    Qwen/Qwen3-VL-8B-Instruct-FP8               L4    32768    pdf_transcriber, image_extractor

Each tier serves its weights under a stable alias (``--served-model-name
mailroom-<tier> <hf-id>``), so the LiteLLM gateway (deploy/litellm/config.yaml)
and Langfuse see ``mailroom-fast`` etc. no matter which checkpoint backs the
tier. Swapping a tier's model is a redeploy with one env knob — no gateway or
pipeline change.

Cost posture: every tier scales to zero (``min_containers=0``) and is capped
at ONE GPU container (``max_containers=1``); vLLM batches concurrent requests
inside that container (``@modal.concurrent``), so a burst never fans out to
more GPUs. Cold start (weights from the cache Volume + CUDA-graph build) is
accepted; the pipeline's retry ladder backs off in minutes on a cold-start
503 (llm/retry.py, DMR-052).

Deploy (all enabled tiers):

    pip install -e ".[deploy]" && modal token new      # once
    export MODAL_VLLM_API_TOKEN="$(openssl rand -hex 24)"
    modal run deploy/modal_vllm.py::download_model --tier fast   # optional pre-warm, per tier
    modal deploy deploy/modal_vllm.py

URLs (stable — set by ``label=``):

    https://<workspace>--mailroom-vllm-fast.modal.run/v1      -> MODAL_FAST_URL
    https://<workspace>--mailroom-vllm-extract.modal.run/v1   -> MODAL_EXTRACT_URL
    https://<workspace>--mailroom-vllm-vision.modal.run/v1    -> MODAL_VISION_URL

Knobs (env at DEPLOY time; baked into the app via ``modal.Secret.from_dict``):

    MODAL_VLLM_TIERS                     comma list of tiers to deploy (default fast,extract,vision)
    MODAL_VLLM_<TIER>_MODEL              HF repo id
    MODAL_VLLM_<TIER>_GPU                Modal GPU string ("L4", "L40S", "A100-80GB:2", ...)
    MODAL_VLLM_<TIER>_MAX_MODEL_LEN      context window (tokens)
    MODAL_VLLM_<TIER>_QUANTIZATION       awq | gptq | fp8 ... (FP8 checkpoints self-describe; leave unset)
    MODAL_VLLM_<TIER>_GPU_MEMORY_UTILIZATION   0.0-1.0 (default 0.90)
    MODAL_VLLM_<TIER>_MAX_NUM_SEQS       vLLM batch slots (default 64)
    MODAL_VLLM_<TIER>_TP_SIZE            tensor-parallel size (default: the GPU ":N" suffix)
    MODAL_VLLM_<TIER>_REVISION           Hub revision SHA (pins weights)
    MODAL_VLLM_<TIER>_REASONING_PARSER   vLLM --reasoning-parser (fast: qwen3; "none" disables)
    MODAL_VLLM_<TIER>_MAX_INPUTS         concurrent HTTP requests per container (default 32)
    MODAL_VLLM_<TIER>_MAX_CONTAINERS     GPU container cap (default 1)
    MODAL_VLLM_<TIER>_MIN_CONTAINERS     warm floor (default 0 = scale to zero)
    MODAL_VLLM_<TIER>_SCALEDOWN_SECONDS  idle window before scale-to-zero (default 900)
    MODAL_VLLM_<TIER>_LIMIT_IMAGES       images per prompt (vision tier; default 10)
    MODAL_VLLM_STARTUP_TIMEOUT_SECONDS   first-boot ceiling for every tier (default 1200)
    MODAL_VLLM_IMAGE_TAG                 vllm/vllm-openai tag (default v0.28.0)
    MODAL_VLLM_API_TOKEN                 bearer token every tier REQUIRES (recommended)
    HF_TOKEN                             gated/private repos (optional)

Cross-repo contract (KANBAN-096): the unscoped single-model knobs
``MODAL_VLLM_MODEL / _GPU / _QUANTIZATION / _MAX_MODEL_LEN / _REVISION /
_GPU_MEMORY_UTILIZATION / _MAX_NUM_SEQS / _TP_SIZE / _SCALEDOWN_SECONDS``
still work: they configure the ``fast`` tier when its scoped knob is unset.

The served API is OpenAI-compatible (/v1/chat/completions, /v1/models).
"""

# No `from __future__ import annotations`: the dataclass below must resolve
# its field types without the module being registered in sys.modules (the
# capability tests load this file via spec_from_file_location).
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import modal

APP_NAME = "mailroom-vllm"
SERVER_PORT = 8000
HF_CACHE_VOLUME_NAME = "mailroom-hf-cache"
VLLM_CACHE_VOLUME_NAME = "mailroom-vllm-cache"
HF_CACHE_MOUNT = "/root/.cache/huggingface"
VLLM_CACHE_MOUNT = "/root/.cache/vllm"

# Pinned for reproducible deploys; bump deliberately (driver/CUDA compat).
VLLM_IMAGE_TAG = os.environ.get("MODAL_VLLM_IMAGE_TAG", "v0.28.0")
STARTUP_TIMEOUT_SECONDS = int(os.environ.get("MODAL_VLLM_STARTUP_TIMEOUT_SECONDS", 20 * 60))

ALL_TIERS = ("fast", "extract", "vision")


@dataclass(frozen=True)
class TierSpec:
    """One GPU tier: a model, the GPU it runs on, and its serve/scale knobs."""

    name: str
    model: str
    gpu: str
    max_model_len: int
    quantization: str = ""
    gpu_memory_utilization: str = "0.90"
    max_num_seqs: int = 64
    tp_size: int = 1
    revision: str = ""
    reasoning_parser: str = ""
    limit_images: int = 0
    max_inputs: int = 32
    max_containers: int = 1
    min_containers: int = 0
    scaledown_seconds: int = 15 * 60

    @property
    def alias(self) -> str:
        """The served model name — the gateway alias in taxonomy.yaml."""
        return f"mailroom-{self.name}"

    @property
    def label(self) -> str:
        """Stable web label -> https://<workspace>--<label>.modal.run."""
        return f"{APP_NAME}-{self.name}"


# Defaults sized for single-GPU, FP8-weight serving (Ada/Hopper FP8):
#   fast    Qwen3-8B-FP8 ~9 GB weights on L4 (24 GB) -> ~10 GB KV, room for
#           several 32k-token sequences. The bf16 checkpoint (16 GB) would
#           leave too little KV for a 32k window on an L4.
#   extract Qwen3-30B-A3B-Instruct-2507-FP8 ~31 GB on L40S (48 GB) -> ~11 GB
#           KV (~100k tokens); the specialists' 100k-char budget (~28k tokens)
#           plus an 8k completion fits a 64k window with margin. Non-thinking
#           checkpoint: no hidden reasoning tokens.
#   vision  Qwen3-VL-8B-Instruct-FP8 on L4; 10 page images x ~1.3k tokens +
#           transcript + 8k completion fits 32k.
_DEFAULTS: dict[str, TierSpec] = {
    "fast": TierSpec(
        name="fast",
        model="Qwen/Qwen3-8B-FP8",
        gpu="L4",
        max_model_len=32768,
        reasoning_parser="qwen3",  # hybrid-thinking model: separate any reasoning
    ),
    "extract": TierSpec(
        name="extract",
        model="Qwen/Qwen3-30B-A3B-Instruct-2507-FP8",
        gpu="L40S",
        max_model_len=65536,
        max_num_seqs=32,
    ),
    "vision": TierSpec(
        name="vision",
        model="Qwen/Qwen3-VL-8B-Instruct-FP8",
        gpu="L4",
        max_model_len=32768,
        max_num_seqs=16,
        limit_images=10,  # == taxonomy vision.max_pages
    ),
}

# Unscoped single-model knobs (KANBAN-096 cross-repo contract) -> fast tier.
_LEGACY_TIER = "fast"


def _knob(tier: str, name: str) -> str | None:
    """Scoped ``MODAL_VLLM_<TIER>_<NAME>`` first; the unscoped legacy knob
    ``MODAL_VLLM_<NAME>`` only for the legacy (fast) tier."""
    scoped = os.environ.get(f"MODAL_VLLM_{tier.upper()}_{name}", "").strip()
    if scoped:
        return scoped
    if tier == _LEGACY_TIER:
        legacy = os.environ.get(f"MODAL_VLLM_{name}", "").strip()
        if legacy:
            return legacy
    return None


def _tp_from_gpu(gpu: str) -> int:
    # A multi-GPU container (e.g. "A100-80GB:2") MUST pass the matching
    # tensor-parallel size or vLLM serves on one GPU and OOMs (DMR-051).
    if ":" in gpu:
        try:
            return int(gpu.rsplit(":", 1)[1])
        except ValueError:
            return 1
    return 1


def resolve_tier(name: str) -> TierSpec:
    """The effective spec for one tier: defaults overlaid with env knobs."""
    if name not in _DEFAULTS:
        raise ValueError(f"unknown tier '{name}' (known: {', '.join(ALL_TIERS)})")
    base = _DEFAULTS[name]
    gpu = _knob(name, "GPU") or base.gpu
    tp = _knob(name, "TP_SIZE")
    parser = _knob(name, "REASONING_PARSER")
    if parser is not None and parser.lower() == "none":
        parser = ""
    return replace(
        base,
        model=_knob(name, "MODEL") or base.model,
        gpu=gpu,
        max_model_len=int(_knob(name, "MAX_MODEL_LEN") or base.max_model_len),
        quantization=_knob(name, "QUANTIZATION") or base.quantization,
        gpu_memory_utilization=_knob(name, "GPU_MEMORY_UTILIZATION") or base.gpu_memory_utilization,
        max_num_seqs=int(_knob(name, "MAX_NUM_SEQS") or base.max_num_seqs),
        tp_size=int(tp) if tp else _tp_from_gpu(gpu),
        revision=_knob(name, "REVISION") or base.revision,
        reasoning_parser=base.reasoning_parser if parser is None else parser,
        limit_images=int(_knob(name, "LIMIT_IMAGES") or base.limit_images),
        max_inputs=int(_knob(name, "MAX_INPUTS") or base.max_inputs),
        max_containers=int(_knob(name, "MAX_CONTAINERS") or base.max_containers),
        min_containers=int(_knob(name, "MIN_CONTAINERS") or base.min_containers),
        scaledown_seconds=int(_knob(name, "SCALEDOWN_SECONDS") or base.scaledown_seconds),
    )


def enabled_tiers() -> list[str]:
    raw = os.environ.get("MODAL_VLLM_TIERS", "").strip()
    names = [t.strip() for t in raw.split(",") if t.strip()] if raw else list(ALL_TIERS)
    unknown = [t for t in names if t not in _DEFAULTS]
    if unknown:
        raise ValueError(f"MODAL_VLLM_TIERS has unknown tier(s) {unknown} (known: {ALL_TIERS})")
    return names


TIERS: dict[str, TierSpec] = {name: resolve_tier(name) for name in ALL_TIERS}

# Legacy single-model module surface (kept for the KANBAN-064/096 tests and
# the sibling contract): the fast tier's effective values.
MODEL = TIERS[_LEGACY_TIER].model
GPU = TIERS[_LEGACY_TIER].gpu
QUANTIZATION = TIERS[_LEGACY_TIER].quantization
MAX_MODEL_LEN = str(TIERS[_LEGACY_TIER].max_model_len)
REVISION = TIERS[_LEGACY_TIER].revision
GPU_MEMORY_UTILIZATION = TIERS[_LEGACY_TIER].gpu_memory_utilization
MAX_NUM_SEQS = str(TIERS[_LEGACY_TIER].max_num_seqs)
TP_SIZE = str(TIERS[_LEGACY_TIER].tp_size)
SCALEDOWN_SECONDS = TIERS[_LEGACY_TIER].scaledown_seconds


def _config_secrets() -> list[modal.Secret]:
    """Deploy-time knobs as an inline Secret (empty list when none are set).

    The container re-imports this module, so every ``MODAL_VLLM_*`` knob set
    at deploy time must travel with it or the remote TIERS would differ from
    the deployed decorators. SDK 1.5.5 removed ``Secret.from_local``;
    ``Secret.from_local_environ`` raises on missing optional names, so build
    the dict ourselves.
    """
    values = {
        name: value
        for name, value in os.environ.items()
        if (name.startswith("MODAL_VLLM_") or name == "HF_TOKEN") and value
    }
    if not values:
        return []
    return [modal.Secret.from_dict(values)]


hf_cache = modal.Volume.from_name(HF_CACHE_VOLUME_NAME, create_if_missing=True)
# vLLM JIT/CUDA-graph compile artifacts: caching them cuts recompilation on
# cold start from minutes to ~seconds.
vllm_cache = modal.Volume.from_name(VLLM_CACHE_VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.from_registry(f"vllm/vllm-openai:{VLLM_IMAGE_TAG}", add_python="3.12")
    # DMR-056: huggingface_hub 1.x uses Xet by default; no [hf_transfer] extra.
    .run_commands("pip install --no-cache-dir huggingface_hub")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)

# Slim image for the pre-warm function (no GPU, no vLLM).
download_image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("huggingface_hub")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)

# DMR-056: cost-allocation tags — parity with the sandbox sibling app.
app = modal.App(
    APP_NAME,
    image=image,
    tags={
        "project": "digital-mailroom",
        "package": "llm-mailroom",
        "purpose": "pipeline-gpu-tiers",
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


def _legacy_spec(model: str) -> TierSpec:
    """A spec from the module-level legacy knobs (read at call time)."""
    return TierSpec(
        name=_LEGACY_TIER,
        model=model,
        gpu=GPU,
        max_model_len=int(MAX_MODEL_LEN),
        quantization=QUANTIZATION,
        gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
        max_num_seqs=int(MAX_NUM_SEQS),
        tp_size=int(TP_SIZE or 1),
        revision=REVISION,
    )


def build_vllm_command(model: str, spec: TierSpec | None = None) -> list[str]:
    """Assemble the `vllm serve` argv. Kept pure for unit testing.

    With ``spec`` the tier's full serve config applies (served alias,
    reasoning parser, image budget); without it the legacy single-model
    knobs do.
    """
    tier = spec or _legacy_spec(model)
    cmd = [
        "vllm",
        "serve",
        model,
        "--host",
        "0.0.0.0",
        "--port",
        str(SERVER_PORT),
        "--max-model-len",
        str(tier.max_model_len),
        "--gpu-memory-utilization",
        str(tier.gpu_memory_utilization),
        "--max-num-seqs",
        str(tier.max_num_seqs),
    ]
    if spec is not None:
        # The alias first (it is what responses report as `model`), the HF id
        # second so direct DEFAULT_PROVIDER=vllm callers still resolve.
        cmd += ["--served-model-name", tier.alias, model]
    if tier.revision:
        # Pin the Hub revision to avoid silent weight changes.
        cmd += ["--revision", tier.revision]
    if tier.quantization:
        cmd += ["--quantization", tier.quantization]
    if tier.tp_size and tier.tp_size != 1:
        cmd += ["--tensor-parallel-size", str(tier.tp_size)]
    if tier.reasoning_parser:
        cmd += ["--reasoning-parser", tier.reasoning_parser]
    if tier.limit_images:
        cmd += ["--limit-mm-per-prompt", f'{{"image": {tier.limit_images}}}']
    # Legal-document workloads are bursty and latency-tolerant: batch freely.
    cmd += ["--no-enable-log-requests"]
    return cmd


def _masked_config(spec: TierSpec | None = None) -> dict[str, str]:
    """The effective serve config for boot diagnostics — secrets masked."""

    def presence(name: str) -> str:
        return "set" if os.environ.get(name, "").strip() else "unset"

    tier = spec or TIERS[_LEGACY_TIER]
    return {
        "tier": tier.name,
        "alias": tier.alias,
        "model": tier.model,
        "gpu": tier.gpu,
        "image": VLLM_IMAGE_TAG,
        "max_model_len": str(tier.max_model_len),
        "gpu_memory_utilization": str(tier.gpu_memory_utilization),
        "max_num_seqs": str(tier.max_num_seqs),
        "tensor_parallel_size": str(tier.tp_size),
        "quantization": tier.quantization or "unset(checkpoint dtype)",
        "revision": tier.revision or "unset(tip)",
        "reasoning_parser": tier.reasoning_parser or "unset",
        "limit_images": str(tier.limit_images or "unset"),
        "max_inputs": str(tier.max_inputs),
        "max_containers": str(tier.max_containers),
        "min_containers": str(tier.min_containers),
        "scaledown_seconds": str(tier.scaledown_seconds),
        "startup_timeout_seconds": str(STARTUP_TIMEOUT_SECONDS),
        "VLLM_API_KEY": presence("MODAL_VLLM_API_TOKEN"),
        "HF_TOKEN": presence("HF_TOKEN"),
    }


def _launch(tier_name: str) -> None:
    spec = resolve_tier(tier_name)
    cmd = build_vllm_command(spec.model, spec)
    # Boot diagnostics (masked): the container log shows the EFFECTIVE config
    # so a failed cold start is diagnosable without re-deriving env (DMR-053).
    print(f"=== mailroom-vllm [{tier_name}] serve config (masked) ===")
    for key, value in _masked_config(spec).items():
        print(f"  {key}: {value}")
    print("starting:", " ".join(cmd))  # never contains secret values
    subprocess.Popen(cmd, env={**os.environ, **_server_env()})


def _tier_function(spec: TierSpec):
    """Decorator stack for one tier's web server (Modal vLLM example order:
    function -> concurrent -> web_server)."""

    def deco(fn):
        fn = modal.web_server(
            port=SERVER_PORT, startup_timeout=STARTUP_TIMEOUT_SECONDS, label=spec.label
        )(fn)
        fn = modal.concurrent(max_inputs=spec.max_inputs)(fn)
        return app.function(
            gpu=spec.gpu,
            volumes={HF_CACHE_MOUNT: hf_cache, VLLM_CACHE_MOUNT: vllm_cache},
            secrets=_config_secrets(),
            timeout=60 * 30,  # per request
            scaledown_window=spec.scaledown_seconds,
            min_containers=spec.min_containers,
            max_containers=spec.max_containers,
            startup_timeout=STARTUP_TIMEOUT_SECONDS,
        )(fn)

    return deco


# Modal registers functions by their global name, so each tier is an explicit
# module-level function (dynamically generated names would need
# serialized=True). MODAL_VLLM_TIERS decides which ones exist in a deploy.
_ENABLED = enabled_tiers()

if "fast" in _ENABLED:

    @_tier_function(TIERS["fast"])
    def serve_fast() -> None:
        _launch("fast")


if "extract" in _ENABLED:

    @_tier_function(TIERS["extract"])
    def serve_extract() -> None:
        _launch("extract")


if "vision" in _ENABLED:

    @_tier_function(TIERS["vision"])
    def serve_vision() -> None:
        _launch("vision")


@app.function(
    image=download_image,
    volumes={HF_CACHE_MOUNT: hf_cache},
    secrets=_config_secrets(),
    timeout=60 * 45,
)
def download_model(model: str = "", revision: str = "", tier: str = "") -> None:
    """Pre-warm the HF cache Volume so the first serve boot skips downloads.

    ``modal run deploy/modal_vllm.py::download_model --tier extract``
    (or ``--model <hf-id>``; no flags pre-warms the fast tier).

    Fails loudly when nothing was cached (DMR-053).
    """
    from huggingface_hub import snapshot_download

    spec = resolve_tier(tier or _LEGACY_TIER)
    model = model or spec.model
    revision = revision or spec.revision or None
    print(f"pre-warming {model}" + (f"@{revision}" if revision else ""))
    # DMR-056: huggingface_hub 1.x returns the snapshot DIRECTORY (str).
    snapshot_dir = snapshot_download(repo_id=model, revision=revision)
    n_files = sum(1 for _ in Path(snapshot_dir).rglob("*")) if snapshot_dir else 0
    if not n_files:
        raise SystemExit(
            f"snapshot_download returned an empty snapshot for {model} — check the "
            "repo id, the revision, and HF_TOKEN for gated repos"
        )
    hf_cache.commit()
    print(f"cached {model}" + (f"@{revision}" if revision else "") + f" ({n_files} file(s))")


def tier_url_env(tier: str) -> str:
    """The env var the gateway + smoke script read for a tier's /v1 URL."""
    return f"MODAL_{tier.upper()}_URL"


@app.local_entrypoint()
def main(check: bool = False, debug: bool = False) -> None:
    """`modal run modal_vllm.py [--check] [--debug]` — guidance + probes."""
    name = Path(__file__).name
    print(f"Deploy with:  modal deploy {name}")
    print(f"Image: vllm/vllm-openai:{VLLM_IMAGE_TAG}   tiers: {', '.join(_ENABLED)}")
    for tier in _ENABLED:
        spec = TIERS[tier]
        url = os.environ.get(tier_url_env(tier), "").rstrip("/")
        print(
            f"  [{tier}] {spec.model} on {spec.gpu} ctx={spec.max_model_len} "
            f"alias={spec.alias} -> {tier_url_env(tier)}="
            f"{url or f'https://<workspace>--{spec.label}.modal.run/v1'}"
        )
        if debug:
            for key, value in _masked_config(spec).items():
                print(f"      {key}: {value}")
        if check:
            _smoke_check(url, expect=spec.alias, label=tier_url_env(tier))
    print(
        "Then point the gateway at the tiers (.env for deploy/docker-compose.full.yml):\n"
        "  MODAL_FAST_URL=https://<workspace>--mailroom-vllm-fast.modal.run/v1\n"
        "  MODAL_EXTRACT_URL=https://<workspace>--mailroom-vllm-extract.modal.run/v1\n"
        "  MODAL_VISION_URL=https://<workspace>--mailroom-vllm-vision.modal.run/v1\n"
        "  MODAL_VLLM_API_TOKEN=<the token deployed above>"
    )


def _smoke_check(base: str, expect: str = "", label: str = "VLLM_BASE_URL") -> None:
    """Bearer-aware `/models` probe for `modal run ... --check` (DMR-053)."""
    import httpx

    if not base:
        raise SystemExit(
            f"{label} is not set — export the URL printed by `modal deploy` "
            "(https://<workspace>--mailroom-vllm-<tier>.modal.run/v1)"
        )
    token = (
        os.environ.get("MODAL_VLLM_API_TOKEN", "").strip()
        or os.environ.get("VLLM_API_KEY", "").strip()
    )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        # Long timeout: the probe itself may trigger a cold start.
        resp = httpx.get(f"{base}/models", headers=headers, timeout=600.0)
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"probe failed: {type(exc).__name__}: {exc}\n"
            "hints: is the app deployed (`modal app list`)? is the URL the "
            "modal.run URL, not localhost?"
        ) from exc
    if resp.status_code == 401:
        raise SystemExit(
            f"401 from {base}/models — the server enforces a bearer token; set "
            "MODAL_VLLM_API_TOKEN to the deployed value"
        )
    if resp.status_code >= 400:
        raise SystemExit(f"HTTP {resp.status_code} from {base}/models\nresponse body: {resp.text[:400]!r}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SystemExit(
            f"{base}/models returned non-JSON (HTTP {resp.status_code}): {resp.text[:400]!r}"
        ) from exc
    ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
    print(f"ok: {base}/models -> {ids}")
    if expect and expect not in ids:
        print(f"note: served model(s) {ids} do not include the tier alias '{expect}' — the gateway will 404")
