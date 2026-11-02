"""Local vs API serving comparison — TTFT, throughput, utilization, identity.

Comparable LLM-serving metrics computed from recorded timings and token
counts (vLLM / NVIDIA NIM / OpenAI streaming conventions). Identity
fields (model, quantization, GPU, provider) are registered, not scored
as quality.

Honesty
-------
* **TTFT** is ``None`` unless a first-token timestamp or explicit
  ``ttft_seconds`` is present. Do not infer TTFT from e2e / n_tokens.
* **GPU / KV-cache utilization** are local-only. API-key providers
  (OpenRouter, …) cannot supply them — the comparison records that gap.
* **Local cost** is ``None`` when the model slug has no OpenRouter price
  table entry (Ollama tags like ``qwen3:8b``). Do not fabricate electricity.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .bootstrap import bootstrap_ci, delta_significance
from .cost import estimate_cost, price_for
from .scorecard_honesty import COST_BASIS_TOKEN_PRICE

__all__ = [
    "LOCAL_PROVIDERS",
    "API_PROVIDERS",
    "SERVING_METRIC_NAMES",
    "IDENTITY_FIELDS",
    "ServingIdentity",
    "ServingObservation",
    "ServingRun",
    "classify_serving_kind",
    "normalize_serving_record",
    "score_serving_run",
    "aggregate_serving",
    "compare_serving",
    "split_local_api",
    "pair_comparable_runs",
    "CANONICAL_SERVING_KEYS",
    "serving_table_rows",
    "serving_table_markdown",
    "serving_cost_card",
    "serving_scorecard",
    "serving_card_markdown",
    "emit_serving_scorecard",
]

LOCAL_PROVIDERS = frozenset(
    {
        "ollama",
        "vllm",
        "vllm-local",
        "llamacpp",
        "llama.cpp",
        "lmstudio",
        "lm-studio",
        "generic",
    }
)
API_PROVIDERS = frozenset(
    {
        "openrouter",
        "openai",
        "anthropic",
        "together",
        "groq",
        "fireworks",
        "deepseek",
        "mistral",
    }
)

# Registry names this module computes (T0/T1 serving surface).
SERVING_METRIC_NAMES: tuple[str, ...] = (
    "ttft_seconds",
    "tokens_per_second",
    "tpot_seconds",
    "e2e_latency_seconds",
    "ttft_p50",
    "ttft_p95",
    "e2e_p50",
    "e2e_p95",
    "output_tokens_per_second",
    "prompt_tokens_per_second",
    "requests_per_second",
    "docs_per_second",
    "gpu_utilization",
    "kv_cache_utilization",
    "gpu_memory_used_gb",
    "queue_time_seconds",
    "error_rate",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "cost_per_document",
)

IDENTITY_FIELDS: tuple[str, ...] = (
    "serving_kind",
    "provider",
    "model",
    "quantization",
    "dtype",
    "max_model_len",
    "gpu",
    "gpu_count",
    "tensor_parallel",
    "profile",
    "prompt_version",
    "task",
    "base_url_host",
)

#: Keys a comparable sandbox / experiment-log record should emit. Missing
#: timings stay ``None`` — they are never inferred from e2e / n_tokens.
CANONICAL_SERVING_KEYS: tuple[str, ...] = (
    "serving_kind",
    "provider",
    "profile",
    "model",
    "quantization",
    "dtype",
    "max_model_len",
    "gpu",
    "gpu_count",
    "tensor_parallel",
    "prompt_version",
    "task",
    "dataset_fingerprint",
    "base_url",
    "ttft_seconds",
    "t_start",
    "t_first_token",
    "t_end",
    "e2e_latency_seconds",
    "queue_time_seconds",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "gpu_utilization",
    "kv_cache_utilization",
    "gpu_memory_used_gb",
    "error",
    "n",
    "estimated_cost_usd",
    "scores",
    "requests",
)

_QUALITY_KEYS = (
    "extraction_f1",
    "f1_macro",
    "exact_match",
    "accuracy",
    "overall_extraction_score",
    "extraction_overall_score",
)


def _first(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    """Return the first nonempty value found by direct or dotted key lookup."""
    if not mapping:
        return None
    for key in keys:
        if "." in key:
            cur: Any = mapping
            ok = True
            for part in key.split("."):
                if isinstance(cur, Mapping) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
            continue
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _as_float(value: Any) -> float | None:
    """Coerce numeric values to floats, rejecting blanks, booleans, and bad input."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """Coerce a float-compatible value to an integer, or return None if absent."""
    num = _as_float(value)
    if num is None:
        return None
    return int(num)


def _as_str(value: Any) -> str | None:
    """Return a stripped string, or None for absent or blank values."""
    if value is None or value == "":
        return None
    return str(value).strip() or None


def _parse_ts(value: Any) -> float | None:
    """Unix seconds from a timestamp, ISO-8601 string, or numeric."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    num = _as_float(value)
    if num is not None:
        # Heuristic: ms since epoch
        if num > 1e12:
            return num / 1000.0
        return num
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _percentile(values: Sequence[float], p: float) -> float | None:
    """Linear interpolation percentile. ``p`` in ``[0, 1]``."""
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return None
    xs.sort()
    if len(xs) == 1:
        return round(xs[0], 6)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    if lo == hi:
        return round(xs[lo], 6)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 6)


def _mean(values: Sequence[float | None]) -> float | None:
    """Average non-None values to six decimals, returning None for no samples."""
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 6)


def _host(url: Any) -> str | None:
    """Extract a hostname from a URL or a scheme-less host and path."""
    text = _as_str(url)
    if not text:
        return None
    if "://" not in text:
        return text.split("/")[0].split(":")[0] or None
    return urlparse(text).hostname


def _frac(value: Any) -> float | None:
    """Normalize 0–1 or 0–100 utilization to ``[0, 1]``."""
    num = _as_float(value)
    if num is None:
        return None
    if num > 1.0:
        num = num / 100.0
    if num < 0:
        return None
    return round(min(num, 1.0), 6)


def classify_serving_kind(
    record: Mapping[str, Any] | None,
    *,
    provider: str | None = None,
) -> str:
    """``local`` | ``api`` | ``modal`` | ``unknown`` — no modal→local remap (#21)."""
    raw = _first(record or {}, "serving_kind", "serving.kind")
    if raw:
        token = str(raw).strip().lower()
        if token in {"modal", "modal-vllm", "modal_vllm"}:
            return "modal"
        if token in {"local", "offline", "self-hosted", "on-prem"}:
            return "local"
        if token in {"api", "cloud", "hosted", "openrouter"}:
            return "api"
    prov = (provider or _as_str(_first(record or {}, "provider", "profile")) or "").lower()
    if prov in {"modal", "modal-vllm"} or prov.startswith("modal"):
        return "modal"
    if prov in LOCAL_PROVIDERS or prov.startswith("ollama") or prov.startswith("vllm"):
        return "local"
    if prov in API_PROVIDERS or prov.startswith("openrouter"):
        return "api"
    profile = (_as_str(_first(record or {}, "profile")) or "").lower()
    if profile in {"modal", "modal-vllm", "modal_vllm"} or profile.startswith("modal"):
        return "modal"
    if profile in LOCAL_PROVIDERS or profile.startswith("ollama") or profile in {
        "vllm-local",
        "llamacpp",
        "lmstudio",
    }:
        return "local"
    if profile in API_PROVIDERS or profile == "openrouter":
        return "api"
    return "unknown"


@dataclass(frozen=True)
class ServingIdentity:
    """Model / quantization / hardware identity for one serving run."""

    serving_kind: str = "unknown"
    provider: str | None = None
    model: str | None = None
    quantization: str | None = None
    dtype: str | None = None
    max_model_len: int | None = None
    gpu: str | None = None
    gpu_count: int | None = None
    tensor_parallel: int | None = None
    profile: str | None = None
    prompt_version: str | None = None
    task: str | None = None
    base_url_host: str | None = None
    dataset_fingerprint: str | None = None
    dataset_revision: str | None = None
    split: str | None = None
    draw_seed: str | None = None
    experiment_name: str | None = None
    n: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the serving identity's dataclass fields as a dictionary."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class ServingObservation:
    """One request's recorded timings / tokens / utilization."""

    ttft_seconds: float | None = None
    e2e_latency_seconds: float | None = None
    tpot_seconds: float | None = None
    queue_time_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    gpu_utilization: float | None = None
    kv_cache_utilization: float | None = None
    gpu_memory_used_gb: float | None = None
    error: bool = False


@dataclass
class ServingRun:
    """Normalized run identity, observations, quality scores, costs, and data gaps."""

    identity: ServingIdentity
    observations: list[ServingObservation] = field(default_factory=list)
    scores: dict[str, Any] = field(default_factory=dict)
    estimated_cost_usd: float | None = None
    honest_gaps: list[str] = field(default_factory=list)
    #: Basis of ``estimated_cost_usd`` (one of ``COST_BASIS_VALUES``), or
    #: ``None`` when a recorded cost carries no label.
    cost_basis: str | None = None
    #: Quality ``metric_id`` the record was scored under (#17), if stamped.
    metric_id: str | None = None


def _identity_from(record: Mapping[str, Any]) -> ServingIdentity:
    """Normalize serving identity aliases, preferring top-level record values."""
    serving = record.get("serving") if isinstance(record.get("serving"), Mapping) else {}
    vllm = record.get("vllm") if isinstance(record.get("vllm"), Mapping) else {}
    merged: dict[str, Any] = {}
    merged.update(serving or {})
    merged.update(vllm or {})
    merged.update(dict(record))
    provider = _as_str(_first(merged, "provider", "DEFAULT_PROVIDER"))
    kind = classify_serving_kind(record, provider=provider)
    n = _as_int(_first(merged, "n", "n_rows", "n_docs"))
    return ServingIdentity(
        serving_kind=kind,
        provider=provider,
        model=_as_str(_first(merged, "model", "model_slug", "llm_model")),
        quantization=_as_str(
            _first(merged, "quantization", "quant", "serving.quantization", "vllm.quantization")
        ),
        dtype=_as_str(_first(merged, "dtype", "serving.dtype")),
        max_model_len=_as_int(
            _first(merged, "max_model_len", "max_model_length", "context_length")
        ),
        gpu=_as_str(_first(merged, "gpu", "gpu_name", "serving.gpu")),
        gpu_count=_as_int(_first(merged, "gpu_count", "tensor_parallel_size")),
        tensor_parallel=_as_int(_first(merged, "tensor_parallel", "tp", "tensor_parallel_size")),
        profile=_as_str(_first(merged, "profile", "SANDBOX_PROFILE")),
        prompt_version=_as_str(_first(merged, "prompt_version", "prompt")),
        task=_as_str(_first(merged, "task", "eval_task")),
        base_url_host=_host(_first(merged, "base_url", "VLLM_BASE_URL", "OLLAMA_BASE_URL")),
        dataset_fingerprint=_as_str(_first(merged, "dataset_fingerprint", "fingerprint")),
        dataset_revision=_as_str(_first(merged, "dataset_revision")),
        split=_as_str(_first(merged, "split")),
        draw_seed=_as_str(_first(merged, "draw_seed")),
        experiment_name=_as_str(_first(merged, "experiment_name", "name", "run_id")),
        n=n,
    )


def _observation_from(row: Mapping[str, Any]) -> ServingObservation:
    """Normalize request telemetry and derive timings only from recorded inputs."""
    tokens = row.get("tokens") if isinstance(row.get("tokens"), Mapping) else {}
    usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
    timings = row.get("timings") if isinstance(row.get("timings"), Mapping) else {}
    merged: dict[str, Any] = {}
    for part in (timings, tokens, usage, row):
        if isinstance(part, Mapping):
            merged.update(part)

    start = _parse_ts(
        _first(merged, "t_start", "start_ts", "request_start", "started_at")
    )
    first = _parse_ts(
        _first(merged, "t_first_token", "first_token_ts", "first_token_at")
    )
    end = _parse_ts(_first(merged, "t_end", "end_ts", "finished_at", "completed_at"))

    ttft = _as_float(
        _first(merged, "ttft_seconds", "ttft", "time_to_first_token")
    )
    if ttft is None and start is not None and first is not None:
        ttft = first - start
        if ttft < 0:
            ttft = None

    e2e = _as_float(
        _first(
            merged,
            "e2e_latency_seconds",
            "latency_seconds",
            "duration_seconds",
            "run_duration_seconds",
            "elapsed_seconds",
            "latency",
        )
    )
    if e2e is None and start is not None and end is not None:
        e2e = end - start
        if e2e < 0:
            e2e = None

    prompt_tok = _as_int(
        _first(merged, "prompt_tokens", "input_tokens", "prompt_token_count")
    )
    completion_tok = _as_int(
        _first(merged, "completion_tokens", "output_tokens", "completion_token_count")
    )
    total_tok = _as_int(_first(merged, "total_tokens"))
    if total_tok is None and prompt_tok is not None and completion_tok is not None:
        total_tok = prompt_tok + completion_tok

    tpot = _as_float(
        _first(merged, "tpot_seconds", "tpot", "time_per_output_token", "itl_seconds")
    )
    if tpot is None and ttft is not None and e2e is not None and completion_tok is not None:
        denom = completion_tok - 1
        if denom > 0 and e2e >= ttft:
            tpot = (e2e - ttft) / denom

    mem = _as_float(
        _first(merged, "gpu_memory_used_gb", "gpu_memory_gb", "mem_used_gb")
    )
    mem_mb = _as_float(_first(merged, "gpu_memory_used_mb"))
    if mem is None and mem_mb is not None:
        mem = mem_mb / 1024.0

    err = _first(merged, "error", "failed", "is_error")
    error = bool(err) if err not in (None, "", 0, "0") else False

    return ServingObservation(
        ttft_seconds=round(ttft, 6) if ttft is not None else None,
        e2e_latency_seconds=round(e2e, 6) if e2e is not None else None,
        tpot_seconds=round(tpot, 6) if tpot is not None else None,
        queue_time_seconds=_as_float(
            _first(merged, "queue_time_seconds", "queue_time", "waiting_time")
        ),
        prompt_tokens=prompt_tok,
        completion_tokens=completion_tok,
        total_tokens=total_tok,
        gpu_utilization=_frac(
            _first(merged, "gpu_utilization", "gpu_util", "nvidia.utilization")
        ),
        kv_cache_utilization=_frac(
            _first(merged, "kv_cache_utilization", "cache_usage", "prefix_cache_hit_rate")
        ),
        gpu_memory_used_gb=round(mem, 6) if mem is not None else None,
        error=error,
    )


def _request_rows(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Find the first nonempty request-row list, or treat the record as one row."""
    for key in ("requests", "observations", "generations", "timings", "per_call"):
        val = record.get(key)
        if isinstance(val, list) and val and isinstance(val[0], Mapping):
            return list(val)
    return [record]


def normalize_serving_record(record: Mapping[str, Any] | ServingRun) -> ServingRun:
    """Normalize a sandbox / experiment-log / raw serving dict into a run.

    Missing costs may be estimated when every observation records at least one
    token-count component; recorded whole-run costs remain authoritative.
    """
    if isinstance(record, ServingRun):
        return record
    identity = _identity_from(record)
    observations = [_observation_from(row) for row in _request_rows(record)]
    scores = record.get("scores") if isinstance(record.get("scores"), Mapping) else {}
    cost = _as_float(
        _first(record, "estimated_cost_usd", "cost_usd", "cost", "scores.estimated_cost_usd")
    )
    cost_basis = _as_str(_first(record, "cost_basis", "usd_basis")) if cost is not None else None
    if cost is None:
        prompt = sum(o.prompt_tokens or 0 for o in observations) or None
        completion = sum(o.completion_tokens or 0 for o in observations) or None
        # Keep one-component pricing, but never price wholly unobserved requests.
        # An explicit total_tokens: 0 is observed zero usage, not a missing
        # component: it can contribute zero to an otherwise complete run and
        # proves a fully zero-token run costs zero when the model is priced.
        complete_usage = all(
            o.prompt_tokens is not None
            or o.completion_tokens is not None
            or o.total_tokens == 0
            for o in observations
        )
        if complete_usage and (prompt or completion):
            cost = estimate_cost(prompt, completion, identity.model)
            if cost is not None:
                cost_basis = COST_BASIS_TOKEN_PRICE
        elif complete_usage and observations and all(
            o.total_tokens == 0 for o in observations
        ) and price_for(identity.model) is not None:
            cost = 0.0
            cost_basis = COST_BASIS_TOKEN_PRICE
    gaps: list[str] = []
    if not any(o.ttft_seconds is not None for o in observations):
        gaps.append("ttft_seconds missing — not inferred from e2e/n_tokens")
    if identity.serving_kind == "api":
        # API-key providers cannot supply GPU / KV / VRAM — drop even if
        # a caller accidentally copied local hardware fields onto the record.
        observations = [
            replace(
                o,
                gpu_utilization=None,
                kv_cache_utilization=None,
                gpu_memory_used_gb=None,
            )
            for o in observations
        ]
        gaps.append("gpu_utilization unavailable on API-key providers")
        gaps.append("kv_cache_utilization unavailable on API-key providers")
    if cost is None and identity.serving_kind == "local":
        gaps.append("estimated_cost_usd unknown for local model slug (no price table)")
    return ServingRun(
        identity=identity,
        observations=observations,
        scores=dict(scores or {}),
        estimated_cost_usd=cost,
        honest_gaps=gaps,
        cost_basis=cost_basis,
        metric_id=_record_metric_id(record),
    )


def _derived(obs: ServingObservation) -> dict[str, float | None]:
    """Calculate token rates where the recorded timings and token counts allow."""
    e2e = obs.e2e_latency_seconds
    ttft = obs.ttft_seconds
    prompt = obs.prompt_tokens
    completion = obs.completion_tokens
    tokens_per_sec = None
    if e2e and e2e > 0 and completion:
        tokens_per_sec = completion / e2e
    output_tps = None
    decode = None
    if ttft is not None and e2e is not None and e2e > ttft:
        decode = e2e - ttft
    if decode and decode > 0 and completion:
        output_tps = completion / decode
    prompt_tps = None
    if ttft and ttft > 0 and prompt:
        prompt_tps = prompt / ttft
    return {
        "tokens_per_second": round(tokens_per_sec, 6) if tokens_per_sec is not None else None,
        "output_tokens_per_second": round(output_tps, 6) if output_tps is not None else None,
        "prompt_tokens_per_second": round(prompt_tps, 6) if prompt_tps is not None else None,
    }


def score_serving_run(run: Mapping[str, Any] | ServingRun) -> dict[str, Any]:
    """Aggregate serving metrics for one local or API run."""
    return aggregate_serving([run])


def aggregate_serving(
    runs: Iterable[Mapping[str, Any] | ServingRun],
) -> dict[str, Any]:
    """Mean + p50/p95 over every observation; costs require every run to be known."""
    runs = list(runs)
    normalized = [normalize_serving_record(r) for r in runs]
    obs: list[ServingObservation] = []
    for run in normalized:
        obs.extend(run.observations)
    ttfts = [o.ttft_seconds for o in obs]
    e2es = [o.e2e_latency_seconds for o in obs]
    tpots = [o.tpot_seconds for o in obs]
    queues = [o.queue_time_seconds for o in obs]
    derived = [_derived(o) for o in obs]
    n_req = len(obs)
    n_err = sum(1 for o in obs if o.error)
    prompt_sum = sum(o.prompt_tokens or 0 for o in obs)
    completion_sum = sum(o.completion_tokens or 0 for o in obs)
    total_sum = sum(
        (o.total_tokens if o.total_tokens is not None else (o.prompt_tokens or 0) + (o.completion_tokens or 0))
        for o in obs
    )
    wall = _mean(e2es)
    rps = None
    if wall and wall > 0 and n_req:
        # requests / mean e2e approximates sequential throughput; when n
        # observations share one wall clock, prefer sum(e2e) as the window.
        window = sum(v for v in e2es if v is not None)
        if window and window > 0:
            rps = round(n_req / window, 6)
    docs = None
    n_docs = sum(r.identity.n or 0 for r in normalized) or n_req
    if wall and wall > 0 and n_docs:
        window = sum(v for v in e2es if v is not None) or (wall * n_req)
        if window > 0:
            docs = round(n_docs / window, 6)

    costs = [r.estimated_cost_usd for r in normalized]
    known_costs = [c for c in costs if c is not None]
    cost = sum(known_costs) if costs and len(known_costs) == len(costs) else None
    cost_per = None
    if cost is not None and n_docs:
        cost_per = round(cost / n_docs, 8)

    identities = [r.identity.as_dict() for r in normalized]
    gaps: list[str] = []
    for run in normalized:
        for gap in run.honest_gaps:
            if gap not in gaps:
                gaps.append(gap)

    metric_ids = [r.metric_id for r in normalized if r.metric_id]

    quality: dict[str, Any] = {}
    for key in _QUALITY_KEYS:
        vals = []
        for run in normalized:
            v = run.scores.get(key)
            num = _as_float(v)
            if num is not None:
                vals.append(num)
        if vals:
            quality[key] = _mean(vals)

    out = {
        "n_requests": n_req,
        "n_runs": len(normalized),
        "n_docs": n_docs,
        "identity": identities[0] if len(identities) == 1 else identities,
        "ttft_seconds": _mean(ttfts),
        "ttft_p50": _percentile([v for v in ttfts if v is not None], 0.50),
        "ttft_p95": _percentile([v for v in ttfts if v is not None], 0.95),
        "tpot_seconds": _mean(tpots),
        "e2e_latency_seconds": _mean(e2es),
        "e2e_p50": _percentile([v for v in e2es if v is not None], 0.50),
        "e2e_p95": _percentile([v for v in e2es if v is not None], 0.95),
        "queue_time_seconds": _mean(queues),
        "tokens_per_second": _mean([d["tokens_per_second"] for d in derived]),
        "output_tokens_per_second": _mean([d["output_tokens_per_second"] for d in derived]),
        "prompt_tokens_per_second": _mean([d["prompt_tokens_per_second"] for d in derived]),
        "requests_per_second": rps,
        "docs_per_second": docs,
        "gpu_utilization": _mean([o.gpu_utilization for o in obs]),
        "kv_cache_utilization": _mean([o.kv_cache_utilization for o in obs]),
        "gpu_memory_used_gb": _mean([o.gpu_memory_used_gb for o in obs]),
        "error_rate": round(n_err / n_req, 6) if n_req else None,
        "prompt_tokens": prompt_sum or None,
        "completion_tokens": completion_sum or None,
        "total_tokens": total_sum or None,
        "estimated_cost_usd": cost,
        "cost_per_document": cost_per,
        "ttft_ci": bootstrap_ci([v for v in ttfts if v is not None]),
        "e2e_ci": bootstrap_ci([v for v in e2es if v is not None]),
        "quality": quality,
        "honest_gaps": gaps,
    }
    if metric_ids:
        out["metric_id"] = metric_ids[0]
        distinct = sorted(set(metric_ids))
        if len(distinct) > 1 or len(metric_ids) != len(normalized):
            # A leg that mixes metrics (or mixes stamped and unstamped runs)
            # has no single identity to compare on (#17).
            out["metric_id_conflict"] = distinct
    costed = [r for r in normalized if r.estimated_cost_usd is not None]
    bases = {r.cost_basis for r in costed}
    if cost is not None and len(bases) == 1 and None not in bases:
        # One labelled basis across every run; unknown, mixed or unlabelled
        # pooled costs carry no basis and are refused at emission (#22).
        out["cost_basis"] = bases.pop()
    mixed = sorted(
        key
        for key in _LEG_IDENTITY_KEYS
        if len({ident.get(key) for ident in identities}) > 1
    )
    if mixed:
        # The leg pools runs from different datasets / prompts / models; no
        # one identity describes it, so it is neither compared nor stamped.
        out["identity_conflict"] = mixed
    return out


#: Identity fields every run in one comparison leg must share.
_LEG_IDENTITY_KEYS = (
    "serving_kind",
    "model",
    "prompt_version",
    "task",
    "dataset_fingerprint",
    "dataset_revision",
    "split",
    "draw_seed",
)


def _record_metric_id(raw: Mapping[str, Any]) -> str | None:
    """Read a metric ID from the record or its nested quality mapping."""
    mid = raw.get("metric_id")
    q = raw.get("quality")
    if not mid and isinstance(q, Mapping):
        mid = q.get("metric_id")
    return str(mid) if mid else None


def _delta_map(local: Mapping[str, Any], api: Mapping[str, Any]) -> dict[str, Any]:
    """Compare available serving metrics using local-minus-API deltas and ratios."""
    out: dict[str, Any] = {}
    for key in SERVING_METRIC_NAMES:
        a = _as_float(local.get(key))
        b = _as_float(api.get(key))
        if a is None and b is None:
            continue
        delta = None if a is None or b is None else round(a - b, 6)
        ratio = None
        if a is not None and b is not None and b != 0:
            ratio = round(a / b, 6)
        out[key] = {"local": a, "api": b, "delta_local_minus_api": delta, "ratio_local_over_api": ratio}
    return out


_LOCAL_ONLY_METRICS = frozenset(
    {"gpu_utilization", "kv_cache_utilization", "gpu_memory_used_gb"}
)
_HEADLINE_NAMES = ("ttft_seconds", "tokens_per_second")
_COST_NAMES = ("estimated_cost_usd", "cost_per_document", "prompt_tokens", "completion_tokens", "total_tokens")


def _identity_dict(agg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy an aggregate's identity, selecting the first entry when it is a list."""
    ident = (agg or {}).get("identity") or {}
    if isinstance(ident, list):
        ident = ident[0] if ident else {}
    return dict(ident) if isinstance(ident, Mapping) else {}


def _metric_status(local: float | None, api: float | None, name: str) -> str:
    """Classify a metric as missing, local-only, API-only, or compared."""
    if local is None and api is None:
        return "missing"
    if api is None:
        return "local_only" if name in _LOCAL_ONLY_METRICS or local is not None else "missing"
    if local is None:
        return "api_only"
    return "compared"


def _metric_note(name: str, status: str, gaps: Sequence[str]) -> str | None:
    """Explain missing telemetry or return a matching recorded cost gap."""
    if status == "missing":
        if name == "ttft_seconds":
            return "TTFT not recorded — not inferred from e2e/n_tokens"
        if name in _LOCAL_ONLY_METRICS:
            return "not recorded on either side"
        if name in _COST_NAMES:
            return "tokens/cost not recorded"
        return "not recorded"
    if status == "local_only" and name in _LOCAL_ONLY_METRICS:
        return "API-key providers cannot supply GPU/KV/VRAM"
    if status == "local_only" and name in _COST_NAMES:
        return "API cost/tokens not recorded"
    if name == "estimated_cost_usd":
        for gap in gaps:
            if "estimated_cost_usd" in gap:
                return gap
    return None


def serving_table_rows(
    comparison: Mapping[str, Any],
    *,
    include_missing: bool = True,
) -> list[dict[str, Any]]:
    """One row per serving metric, including missing elements as ``None``.

    ``status`` is ``compared`` | ``local_only`` | ``api_only`` | ``missing``.
    Rows are never zero-filled.
    """
    from .registry import load_registry

    left = comparison.get("local") or {}
    right = comparison.get("api") or {}
    gaps = list(comparison.get("honest_gaps") or [])
    try:
        reg = load_registry()
    except Exception:
        reg = None
    rows: list[dict[str, Any]] = []
    for name in SERVING_METRIC_NAMES:
        local_v = _as_float(left.get(name)) if isinstance(left, Mapping) else None
        api_v = _as_float(right.get(name)) if isinstance(right, Mapping) else None
        status = _metric_status(local_v, api_v, name)
        if status == "missing" and not include_missing:
            continue
        delta = None if local_v is None or api_v is None else round(local_v - api_v, 6)
        ratio = None
        if local_v is not None and api_v is not None and api_v != 0:
            ratio = round(local_v / api_v, 6)
        tier = None
        units = None
        if reg is not None and name in getattr(reg, "metrics", {}):
            m = reg.get(name)
            tier = int(m.tier)
            units = m.units
        rows.append(
            {
                "metric": name,
                "tier": tier,
                "units": units,
                "local": local_v,
                "api": api_v,
                "delta_local_minus_api": delta,
                "ratio_local_over_api": ratio,
                "status": status,
                "note": _metric_note(name, status, gaps),
            }
        )
    return rows


def _cell(value: Any) -> str:
    """Format a table value compactly, representing None and NaN as None."""
    if value is None:
        return "None"
    if isinstance(value, float):
        if value != value:  # NaN
            return "None"
        text = f"{value:.6g}"
        return text
    return str(value)


def serving_table_markdown(
    comparison: Mapping[str, Any],
    *,
    include_missing: bool = True,
) -> str:
    """Markdown scoring table: every comparable metric plus missing elements."""
    rows = serving_table_rows(comparison, include_missing=include_missing)
    lines = [
        "| metric | tier | local | api | Δ local−api | ratio | status | note |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        tier = "" if row["tier"] is None else str(row["tier"])
        note = row["note"] or ""
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row['metric']}`",
                    tier,
                    _cell(row["local"]),
                    _cell(row["api"]),
                    _cell(row["delta_local_minus_api"]),
                    _cell(row["ratio_local_over_api"]),
                    row["status"],
                    note.replace("|", "/"),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def serving_cost_card(agg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a cost breakdown, preserving explicitly unknown aggregate totals.

    Only inputs without ``estimated_cost_usd`` use legacy token-price fallback.
    """
    agg = agg or {}
    ident = _identity_dict(agg)
    model = ident.get("model")
    prices = price_for(model)
    prompt = agg.get("prompt_tokens")
    completion = agg.get("completion_tokens")
    recorded = _as_float(agg.get("estimated_cost_usd"))
    explicit_cost = "estimated_cost_usd" in agg
    computed = None if explicit_cost else estimate_cost(prompt, completion, model)
    cost = recorded if explicit_cost else computed
    cost_basis = agg.get("cost_basis")
    if cost_basis is None and recorded is None and computed is not None:
        # Computed here from tokens × the price table, so the basis is known.
        cost_basis = COST_BASIS_TOKEN_PRICE
    formula = None
    if prices is not None and prompt is not None and completion is not None:
        formula = (
            f"({int(prompt)} × {prices[0]}/1e6) + ({int(completion)} × {prices[1]}/1e6)"
        )
    gap = None
    if prices is None:
        gap = "no OpenRouter price table for this model slug (do not fabricate electricity)"
    elif prompt is None and completion is None:
        gap = "token counts not recorded"
    n_docs = agg.get("n_docs")
    cost_per = agg.get("cost_per_document") if cost is not None else None
    if cost_per is None and cost is not None and n_docs:
        cost_per = round(cost / n_docs, 8)
    return {
        "model": model,
        "serving_kind": ident.get("serving_kind"),
        "provider": ident.get("provider"),
        "quantization": ident.get("quantization"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": agg.get("total_tokens"),
        "n_docs": n_docs,
        "price_per_million_prompt": prices[0] if prices else None,
        "price_per_million_completion": prices[1] if prices else None,
        "formula": formula,
        "estimated_cost_usd": cost,
        "cost_per_document": cost_per,
        "cost_basis": cost_basis if cost is not None else None,
        "honest_gap": gap,
    }


def _cost_delta(local_cost: Mapping[str, Any], api_cost: Mapping[str, Any]) -> dict[str, Any]:
    """Compare total and per-document costs without filling missing estimates."""
    a = _as_float(local_cost.get("estimated_cost_usd"))
    b = _as_float(api_cost.get("estimated_cost_usd"))
    pa = _as_float(local_cost.get("cost_per_document"))
    pb = _as_float(api_cost.get("cost_per_document"))
    return {
        "estimated_cost_usd": {
            "local": a,
            "api": b,
            "delta_local_minus_api": None if a is None or b is None else round(a - b, 6),
        },
        "cost_per_document": {
            "local": pa,
            "api": pb,
            "delta_local_minus_api": None if pa is None or pb is None else round(pa - pb, 8),
        },
    }


def serving_scorecard(comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Dashboard scorecard for a local vs API comparison.

    T0 headlines, T0+T1 dashboard (including cost), identity tags, cost
    calculations, and an explicit ``missing`` list of unrecorded metrics.
    """
    rows = serving_table_rows(comparison, include_missing=True)
    by_name = {r["metric"]: r for r in rows}
    headlines = {n: by_name[n] for n in _HEADLINE_NAMES if n in by_name}
    dashboard = {
        r["metric"]: r
        for r in rows
        if r.get("tier") is None or int(r["tier"]) <= 1
    }
    local_cost = serving_cost_card(comparison.get("local") or {})
    api_cost = serving_cost_card(comparison.get("api") or {})
    missing = [r["metric"] for r in rows if r["status"] == "missing"]
    card: dict[str, Any] = {
        "agent": "local_vs_api",
        "headlines": headlines,
        "dashboard": dashboard,
        "identity": {
            "local": _identity_dict(comparison.get("local") or {}),
            "api": _identity_dict(comparison.get("api") or {}),
        },
        "cost": {
            "local": local_cost,
            "api": api_cost,
            "delta": _cost_delta(local_cost, api_cost),
        },
        "quality": comparison.get("quality"),
        "honest_gaps": list(comparison.get("honest_gaps") or []),
        "missing": missing,
        "n_requests": {
            "local": (comparison.get("local") or {}).get("n_requests"),
            "api": (comparison.get("api") or {}).get("n_requests"),
        },
    }
    if comparison.get("incomparable"):
        card["incomparable"] = True
        card["incomparable_reason"] = comparison.get("incomparable_reason")
    return card


def serving_card_markdown(comparison: Mapping[str, Any]) -> str:
    """Markdown scoring card: identity, T0, full metric table, cost, gaps."""
    card = serving_scorecard(comparison)
    loc = card["identity"]["local"]
    api = card["identity"]["api"]
    lines = [
        "# local vs API serving scorecard",
        "",
        "## Identity",
        "",
        "| field | local | api |",
        "|---|---|---|",
    ]
    fields = (
        "serving_kind",
        "provider",
        "model",
        "quantization",
        "dtype",
        "gpu",
        "max_model_len",
        "profile",
        "prompt_version",
        "task",
    )
    for field_name in fields:
        lv = loc.get(field_name)
        av = api.get(field_name)
        if lv is None and av is None:
            continue
        lines.append(f"| `{field_name}` | {_cell(lv)} | {_cell(av)} |")
    if card.get("incomparable"):
        lines.extend([
            "",
            f"**INCOMPARABLE quality** ({card.get('incomparable_reason')}): "
            f"local metric_id={_cell(comparison.get('left_metric_id'))} · "
            f"api metric_id={_cell(comparison.get('right_metric_id'))}. "
            "No quality delta is reported.",
        ])
        for side in ("local", "api"):
            leg = comparison.get(side) or {}
            if leg.get("metric_id_conflict"):
                lines.append(
                    f"- {side} leg mixes metric_ids: "
                    f"{', '.join(map(str, leg['metric_id_conflict']))}"
                )
            if leg.get("identity_conflict"):
                lines.append(
                    f"- {side} leg mixes runs that differ on: "
                    f"{', '.join(leg['identity_conflict'])}"
                )
    lines.extend(["", "## Headlines (T0)", ""])
    for name, row in card["headlines"].items():
        lines.append(
            f"- `{name}`: local={_cell(row['local'])} · api={_cell(row['api'])} · "
            f"Δ={_cell(row['delta_local_minus_api'])} ({row['status']})"
        )
    lines.extend(["", "## Scoring table", "", serving_table_markdown(comparison), ""])
    lc, ac = card["cost"]["local"], card["cost"]["api"]
    lines.extend(
        [
            "## Cost calculations",
            "",
            "| side | model | prompt tok | completion tok | $/1M in | $/1M out | formula | estimated USD | per doc | note |",
            "|---|---|---:|---:|---:|---:|---|---:|---:|---|",
        ]
    )
    for side, block in (("local", lc), ("api", ac)):
        lines.append(
            "| "
            + " | ".join(
                [
                    side,
                    _cell(block.get("model")),
                    _cell(block.get("prompt_tokens")),
                    _cell(block.get("completion_tokens")),
                    _cell(block.get("price_per_million_prompt")),
                    _cell(block.get("price_per_million_completion")),
                    (block.get("formula") or "None").replace("|", "/"),
                    _cell(block.get("estimated_cost_usd")),
                    _cell(block.get("cost_per_document")),
                    (block.get("honest_gap") or "").replace("|", "/"),
                ]
            )
            + " |"
        )
    if card["honest_gaps"]:
        lines.extend(["", "## Honest gaps", ""])
        for gap in card["honest_gaps"]:
            lines.append(f"- {gap}")
    if card["missing"]:
        lines.extend(["", "## Missing elements", ""])
        lines.append(
            "Recorded as `None` (not 0.0): " + ", ".join(f"`{n}`" for n in card["missing"])
        )
    return "\n".join(lines) + "\n"


def emit_serving_scorecard(
    comparison: Mapping[str, Any],
    *,
    run_id: str | None = None,
    emitter: Any | None = None,
    provenance: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist local and API T0/T1 values as separate scorecard runs.

    Local values go to ``run_id:local``; API values to ``run_id:api``.
    Missing values are not emitted (never stored as 0.0).

    ``provenance`` maps ``"local"`` / ``"api"`` to that leg's stamp (#22).
    Keys the caller leaves out fall back to what the leg's records carry:
    ``model_id`` / ``serving_kind`` / ``prompt_id`` from the identity tags
    (``model``, ``serving_kind``, ``prompt_version``); ``dataset_revision`` /
    ``split`` / ``draw_seed`` from the same-named record fields
    (``draw_seed`` also from ``dataset_fingerprint``, as ``stamp_provenance``
    does); ``cost_basis`` from the leg's cost block (price-table USD is
    ``token_price_estimate``). ``dataset_revision`` is never derived from
    anything else: a leg with no revision anywhere is refused before any
    record is written.
    """
    from .emitter import _emit_provenance, get_emitter
    from .scorecard_honesty import _METRIC_ID_BY_NAME, ProvenanceExportError, _absent

    quality_ids = set(_METRIC_ID_BY_NAME.values())

    em = emitter or get_emitter()
    card = serving_scorecard(comparison)
    pending: list[dict[str, Any]] = []
    for side in ("local", "api"):
        sid = f"{run_id}:{side}" if run_id else side
        _refuse_mixed_leg(side, comparison.get(side))
        ident = card["identity"][side]
        side_prov = dict((provenance or {}).get(side) or {})
        if isinstance(side_prov.get("provenance"), Mapping):  # a stamped card
            side_prov = dict(side_prov["provenance"])
        # An explicit None / "" is "not given": fall back to the records.
        side_prov = {k: v for k, v in side_prov.items() if not _absent(v)}
        # A leg stamp may name the leg's quality metric_id (its own, or any
        # quality id when the leg records none); every emitted row carries
        # the canonical id of its own serving metric instead. Any other
        # claimed id stays in and is refused per row as a contradiction.
        leg_metric_id = (comparison.get(side) or {}).get("metric_id")
        claimed = side_prov.get("metric_id")
        if claimed is not None and (
            claimed == leg_metric_id
            or (not leg_metric_id and claimed in quality_ids)
        ):
            side_prov.pop("metric_id")
        side_prov.setdefault("model_id", ident.get("model_id") or ident.get("model"))
        side_prov.setdefault("serving_kind", ident.get("serving_kind") or side)
        side_prov.setdefault("prompt_id", ident.get("prompt_version"))
        for key in ("dataset_revision", "split"):
            if ident.get(key):
                side_prov.setdefault(key, ident[key])
        seed = ident.get("draw_seed") or ident.get("dataset_fingerprint")
        if seed:
            side_prov.setdefault("draw_seed", seed)
        side_basis = (card.get("cost") or {}).get(side, {}).get("cost_basis")
        if side_basis:
            side_prov.setdefault("cost_basis", side_basis)
        for name, row in card["dashboard"].items():
            value = row.get(side)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            try:
                validated_prov = _emit_provenance(
                    name, em.registry.get(name).units, side_prov,
                    require=em.require_provenance,
                )
            except ProvenanceExportError as exc:
                raise ProvenanceExportError(
                    f"emit_serving_scorecard: {side} leg: {exc}. Pass "
                    f"provenance={{'local': {{...}}, 'api': {{...}}}} or record "
                    f"the key on the leg's serving records."
                ) from exc
            pending.append(dict(
                agent_name="local_vs_api",
                doc_id=side,
                metric_name=name,
                value=float(value),
                run_id=sid,
                metadata={"side": side, "status": row.get("status")},
                provenance=validated_prov,
            ))
    # Refuse invalid provenance on either leg before emitting any records.
    for score in pending:
        em.emit_score(**score)
    return card


def compare_serving(
    local: Mapping[str, Any] | ServingRun | Sequence[Any],
    api: Mapping[str, Any] | ServingRun | Sequence[Any],
    *,
    quality_metric: str | None = None,
    require_provenance: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare a local serving run (or list) against an API-key run.

    ``delta_local_minus_api`` is signed: negative TTFT means local is faster.
    GPU/KV metrics stay ``None`` on the API side rather than being zero-filled.

    Quality is compared only when both legs carry the same ``metric_id``
    (#17). A mismatched, one-sided or mixed identity marks the payload
    ``incomparable`` and leaves the quality delta ``None``.
    Two legs with no id at all stay comparable and are flagged
    ``metric_id_unverified``. A leg that pools runs differing on dataset,
    split, seed, prompt, task or model is ``identity_mixed`` (named in the
    leg's ``identity_conflict``).
    ``require_provenance`` (``{"local": {...}, "api": {...}}``, merged over
    what the records carry) runs ``validate_comparison_provenance`` first
    and raises on any gap (#22) — including a metric_id mismatch, which it
    refuses instead of returning an ``incomparable`` payload.

    Also returns ``table`` (every T0/T1 metric including missing ``None``
    rows), ``scorecard`` (headlines + dashboard + identity + cost),
    ``cost`` (token × price-table breakdown), and ``markdown``.
    """
    local_runs: Sequence[Any] = (
        local if isinstance(local, Sequence) and not isinstance(local, (str, bytes, Mapping, ServingRun))
        else [local]
    )
    api_runs: Sequence[Any] = (
        api if isinstance(api, Sequence) and not isinstance(api, (str, bytes, Mapping, ServingRun))
        else [api]
    )
    left = aggregate_serving(local_runs)
    right = aggregate_serving(api_runs)

    quality_key = quality_metric
    if quality_key is None:
        shared = set(left.get("quality") or {}) & set(right.get("quality") or {})
        for cand in _QUALITY_KEYS:
            if cand in shared:
                quality_key = cand
                break

    left_metric_id = left.get("metric_id") or (left.get("quality") or {}).get("metric_id")
    right_metric_id = right.get("metric_id") or (right.get("quality") or {}).get("metric_id")
    reason: str | None = None
    if left.get("metric_id_conflict") or right.get("metric_id_conflict"):
        reason = "metric_id_mixed"
    elif left_metric_id and right_metric_id and left_metric_id != right_metric_id:
        reason = "metric_id_mismatch"
    elif bool(left_metric_id) != bool(right_metric_id):
        # One leg names its metric, the other does not: no shared identity.
        reason = "metric_id_missing"
    elif left.get("identity_conflict") or right.get("identity_conflict"):
        reason = "identity_mixed"
    incomparable: dict[str, Any] | None = None
    if reason:
        incomparable = {
            "comparable": False,
            "incomparable": True,
            "incomparable_reason": reason,
            "left_metric_id": left_metric_id,
            "right_metric_id": right_metric_id,
        }
    if require_provenance:
        from .scorecard_honesty import validate_comparison_provenance

        _refuse_mixed_leg("local", left)
        _refuse_mixed_leg("api", right)
        validate_comparison_provenance(
            _leg_provenance(left, require_provenance.get("local")),
            _leg_provenance(right, require_provenance.get("api")),
            require_cost_basis_if_cost=False,
            vary=("serving_kind", "model_id"),
        )

    quality_block: dict[str, Any] | None = None
    if quality_key:
        lv = (left.get("quality") or {}).get(quality_key)
        rv = (right.get("quality") or {}).get(quality_key)
        quality_block = {
            "metric": quality_key,
            "local": lv,
            "api": rv,
            "delta_local_minus_api": (
                None if incomparable or lv is None or rv is None else round(lv - rv, 6)
            ),
        }
        if incomparable:
            quality_block["status"] = "incomparable"

    local_ttft = [
        o.ttft_seconds
        for r in (normalize_serving_record(x) for x in local_runs)
        for o in r.observations
        if o.ttft_seconds is not None
    ]
    api_ttft = [
        o.ttft_seconds
        for r in (normalize_serving_record(x) for x in api_runs)
        for o in r.observations
        if o.ttft_seconds is not None
    ]
    local_e2e = [
        o.e2e_latency_seconds
        for r in (normalize_serving_record(x) for x in local_runs)
        for o in r.observations
        if o.e2e_latency_seconds is not None
    ]
    api_e2e = [
        o.e2e_latency_seconds
        for r in (normalize_serving_record(x) for x in api_runs)
        for o in r.observations
        if o.e2e_latency_seconds is not None
    ]

    gaps = list(left.get("honest_gaps") or [])
    for gap in right.get("honest_gaps") or []:
        if gap not in gaps:
            gaps.append(gap)

    payload = {
        "local": left,
        "api": right,
        "metrics": _delta_map(left, right),
        "quality": quality_block,
        "ttft_delta": delta_significance(api_ttft, local_ttft),
        "e2e_delta": delta_significance(api_e2e, local_e2e),
        "honest_gaps": gaps,
    }
    if incomparable:
        payload.update(incomparable)
    elif not left_metric_id and not right_metric_id:
        # Comparable by default, but nothing proves the legs share a metric.
        payload["metric_id_unverified"] = True
    payload["table"] = serving_table_rows(payload)
    payload["scorecard"] = serving_scorecard(payload)
    payload["cost"] = payload["scorecard"]["cost"]
    payload["markdown"] = serving_card_markdown(payload)
    return payload


def _refuse_mixed_leg(side: str, agg: Mapping[str, Any] | None) -> None:
    """A leg pooling runs with different identities has no one provenance."""
    mixed = (agg or {}).get("identity_conflict")
    if mixed:
        from .scorecard_honesty import ProvenanceExportError

        raise ProvenanceExportError(
            f"{side} leg pools runs that differ on {', '.join(mixed)}; "
            "compare or emit one identity per leg"
        )


def _leg_provenance(
    agg: Mapping[str, Any], given: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Build one leg's provenance from its aggregate and non-None overrides."""
    ident = _identity_dict(agg)
    prov: dict[str, Any] = {
        "dataset_revision": ident.get("dataset_revision"),
        "split": ident.get("split"),
        "draw_seed": ident.get("draw_seed") or ident.get("dataset_fingerprint"),
        "prompt_id": ident.get("prompt_version"),
        "metric_id": agg.get("metric_id"),
        "serving_kind": ident.get("serving_kind"),
        "model_id": ident.get("model"),
        "cost_basis": agg.get("cost_basis"),
    }
    from .scorecard_honesty import SCORER_VERSION

    prov["scorer_version"] = SCORER_VERSION
    prov.update({k: v for k, v in dict(given or {}).items() if v is not None})
    return prov


def split_local_api(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Partition experiment-log records into local, api, and unknown."""
    local: list[Mapping[str, Any]] = []
    api: list[Mapping[str, Any]] = []
    unknown: list[Mapping[str, Any]] = []
    for rec in records:
        kind = classify_serving_kind(rec)
        if kind == "local":
            local.append(rec)
        elif kind == "api":
            api.append(rec)
        else:
            unknown.append(rec)
    return local, api, unknown


def pair_comparable_runs(
    records: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Pair local/api records that measured the same thing.

    Records pair only when task, prompt, dataset fingerprint, and every
    provenance field they carry (``dataset_revision``, ``split``,
    ``draw_seed``, ``metric_id``) match (#22). A field one record carries and
    the other does not keeps them apart.
    """
    local, api, _ = split_local_api(records)

    def _key(rec: Mapping[str, Any]) -> tuple[Any, ...]:
        """Group comparable records by task, prompt, dataset draw, and metric ID."""
        ident = _identity_from(rec)
        return (
            ident.task,
            ident.prompt_version,
            ident.dataset_fingerprint,
            ident.dataset_revision,
            ident.split,
            ident.draw_seed,
            _record_metric_id(rec),
        )

    api_by: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for rec in api:
        api_by.setdefault(_key(rec), []).append(rec)
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for rec in local:
        bucket = api_by.get(_key(rec)) or []
        if bucket:
            pairs.append((rec, bucket.pop(0)))
    return pairs
