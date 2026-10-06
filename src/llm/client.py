import os
from dataclasses import dataclass

from openai import OpenAI
from .providers import ProviderConfig, resolve_provider
from pipeline.config import get_agent_config, load_config
from pipeline.env import load_env

load_env()


def free_only_enabled() -> bool:
    """Whether the free-only LLM guardrail is on (``MAILROOM_LLM_FREE_ONLY``).

    Opt-in and reversible (HUB-039): on during the Gmail triage pilot so the
    OpenRouter key can ONLY resolve free models; unset/``0`` in full
    production, where paid agents handle multi-document uploads.
    """
    return str(os.environ.get("MAILROOM_LLM_FREE_ONLY", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_free_model(model: str) -> bool:
    """The free-model predicate (shared by the guardrail and the failover
    swarm): a model is free when its taxonomy ``cost_models`` prices are both
    0.0 (the registry is the pricing source of truth) or, unregistered, when
    it carries OpenRouter's ``:free`` suffix convention.
    """
    cost = (load_config().get("cost_models", {}) or {}).get(model) or {}
    try:
        free = float(cost.get("input_per_million", -1)) == 0.0 and float(
            cost.get("output_per_million", -1)
        ) == 0.0
    except (TypeError, ValueError):
        free = False
    return free or str(model).endswith(":free")


def assert_free_model(model: str) -> None:
    """Raise when ``MAILROOM_LLM_FREE_ONLY`` is on and ``model`` is not free.

    No-op when the flag is off. Uses the shared :func:`is_free_model`
    predicate; anything else is refused BEFORE any client (and therefore any
    request) can exist — callers fail soft per their own error paths,
    documents park, nothing spends.
    """
    if not free_only_enabled():
        return
    if not is_free_model(model):
        raise RuntimeError(
            f"MAILROOM_LLM_FREE_ONLY is on: model '{model}' is not free "
            "(cost_models prices non-zero, or unregistered without a ':free' "
            "suffix) — refusing to resolve an LLM client for it. Unset "
            "MAILROOM_LLM_FREE_ONLY for full production."
        )


@dataclass(frozen=True)
class ResolvedModel:
    """What one agent actually calls: endpoint, wire model id, gateway tier.

    ``model`` is the id sent on the wire (self-hosted remap or gateway tier
    alias applied); ``champion`` is the taxonomy ``model:``; ``tier`` is set
    only on the LiteLLM gateway path.
    """

    provider: ProviderConfig
    model: str
    champion: str
    tier: str | None = None

    @property
    def on_gpu_tier(self) -> bool:
        """True when a gateway GPU tier (an aliased tier) serves the call."""
        return self.provider.name == "litellm" and bool(self.tier and gateway_alias(self.tier))

    @property
    def served_by_vllm(self) -> bool:
        """Direct vLLM provider or a gateway GPU tier (Modal vLLM)."""
        return self.provider.name == "vllm" or self.on_gpu_tier

    @property
    def spend_exempt(self) -> bool:
        """No per-token price: self-hosted providers and gateway GPU tiers."""
        return is_free_only_exempt(self.provider.name) or self.on_gpu_tier


def _gateway_config() -> dict:
    """Return the cached taxonomy gateway settings, or an empty mapping."""
    return load_config().get("gateway") or {}


def _tier_overrides() -> dict[str, str]:
    """``MAILROOM_GATEWAY_TIERS="sorter=api,boss=extract"`` → {agent: tier}.

    Read on every call so an operator can flip one agent between the GPU and
    API paths without editing taxonomy.yaml (whose load is cached).
    """
    raw = os.environ.get("MAILROOM_GATEWAY_TIERS", "")
    out: dict[str, str] = {}
    for item in raw.split(","):
        agent, sep, tier = item.partition("=")
        if sep and agent.strip() and tier.strip():
            out[agent.strip()] = tier.strip()
    return out


def gateway_tier(agent_name: str, agent_cfg: dict | None = None) -> str:
    """The gateway tier an agent routes to: env override > ``tier:`` >
    ``gateway.default_tier`` > ``api``. Raise ValueError for unknown tiers
    (a typo must never silently fall through to a paid route). If ``agent_cfg``
    is omitted, an unknown agent raises KeyError from the taxonomy lookup.
    """
    if agent_cfg is None:
        agent_cfg = get_agent_config(agent_name)
    cfg = _gateway_config()
    tiers = cfg.get("tiers") or {}
    tier = (
        _tier_overrides().get(agent_name)
        or agent_cfg.get("tier")
        or cfg.get("default_tier")
        or "api"
    )
    if tier not in tiers:
        raise ValueError(
            f"agent '{agent_name}' routes to unknown gateway tier '{tier}' "
            f"(taxonomy gateway.tiers: {sorted(tiers)})"
        )
    return str(tier)


def gateway_alias(tier: str) -> str | None:
    """Return a tier's configured alias, or None for an unknown or unaliased tier."""
    spec = (_gateway_config().get("tiers") or {}).get(tier) or {}
    alias = spec.get("alias")
    return str(alias) if alias else None


def resolve_agent_model(agent_name: str, agent_cfg: dict | None = None) -> ResolvedModel:
    """Resolve an agent's provider, wire model, champion model, and gateway tier.

    ``agent_cfg`` replaces the agent's taxonomy entry when supplied.
    ``DEFAULT_PROVIDER`` overrides its provider; self-hosted model maps and
    LiteLLM tier aliases determine the wire model. This does not create a
    client or enforce the spend guardrail.

    Propagates KeyError for an unknown agent when no config is supplied, and
    ValueError for an unknown provider or tier, an empty provider URL, or a
    missing/mock OpenRouter key.
    """
    if agent_cfg is None:
        agent_cfg = get_agent_config(agent_name)
    provider, champion = resolve_provider(agent_cfg)
    model, tier = champion, None
    if provider.name in _SELF_HOSTED_PROVIDERS:
        # DMR-052/076: the served model id differs from the taxonomy's
        # OpenRouter champion slug — remap before the client exists so the
        # endpoint never 404s on the champion id.
        model = _self_hosted_model(champion, provider.name)
    elif provider.name == "litellm":
        tier = gateway_tier(agent_name, agent_cfg)
        model = gateway_alias(tier) or champion
    return ResolvedModel(provider=provider, model=model, champion=champion, tier=tier)


def check_spend_guardrail(agent_name: str, resolved: ResolvedModel) -> None:
    """The free-only guardrail bounds OpenRouter spend; self-hosted providers
    and gateway GPU tiers have no per-token price and are exempt (DMR-052).

    When enabled, raise RuntimeError for a non-exempt model that is not free,
    or a non-exempt provider whose nonempty credential variable is neither
    ``OPENROUTER_API_KEY`` nor ``LITELLM_API_KEY``. Otherwise return None.
    """
    if resolved.spend_exempt:
        return
    assert_free_model(resolved.model)
    provider = resolved.provider
    if (
        free_only_enabled()
        and provider.api_key_env
        # The gateway key fronts OpenRouter on the `api` tier; the model was
        # just proven free, so the route cannot spend.
        and provider.api_key_env not in ("OPENROUTER_API_KEY", "LITELLM_API_KEY")
    ):
        raise RuntimeError(
            f"MAILROOM_LLM_FREE_ONLY is on: agent '{agent_name}' resolves "
            f"provider credential '{provider.api_key_env}' outside the "
            "OpenRouter free tier — refusing."
        )


def reasoning_extra_body(effort: str | None, agent_name: str | None = None) -> dict | None:
    """``extra_body`` carrying an agent's reasoning effort, shaped per backend.

    OpenRouter takes ``reasoning.effort``. vLLM ignores that field, and
    hybrid-thinking Qwen3 checkpoints think by default — burning the
    ``max_tokens`` budget the JSON object needs — so vLLM-served calls get
    the chat-template switch instead (``enable_thinking`` off for effort
    ``none``/``minimal``). Resolution failures keep the OpenRouter shape.
    A missing or empty effort returns None.
    """
    if not effort:
        return None
    if agent_name:
        try:
            if resolve_agent_model(agent_name).served_by_vllm:
                thinking = str(effort).lower() not in ("none", "minimal")
                return {"chat_template_kwargs": {"enable_thinking": thinking}}
        except Exception:
            pass
    return {"reasoning": {"effort": effort}}


def get_llm(agent_name: str) -> tuple[OpenAI, str]:
    """Create a tracing-instrumented client and return it with the wire model ID.

    Resolve the agent and enforce the free-only guardrail before creating the
    client. Propagates resolution errors (KeyError/ValueError), guardrail
    RuntimeError, and client construction or instrumentation errors. Providers
    without a supplied key use ``not-needed``; OpenRouter resolution requires
    a non-placeholder key.
    """
    resolved = resolve_agent_model(agent_name, get_agent_config(agent_name))
    check_spend_guardrail(agent_name, resolved)
    provider = resolved.provider
    kwargs = {"base_url": provider.base_url, "api_key": "not-needed"}
    if provider.api_key_env:
        key = os.environ.get(provider.api_key_env)
        if key:
            kwargs["api_key"] = key
    client = OpenAI(**kwargs)
    client = instrument_client(client)
    return client, resolved.model


#: Provider names with a self-hosted served-model remap (taxonomy
#: ``<provider>_model_map``). The served id differs from the OpenRouter
#: champion slug for all of these (vLLM serves HF ids; ollama/llamafile
#: serve their own tag/alias ids).
_SELF_HOSTED_PROVIDERS = ("vllm", "ollama", "llamafile")


def _self_hosted_model(model: str, provider: str = "vllm") -> str:
    """Remap an OpenRouter champion id to the served id for self-hosted
    endpoints (vLLM / ollama / llamafile).

    The map lives in ``taxonomy.yaml: <provider>_model_map`` (single source
    of truth; DMR-052 for vllm, DMR-076 for ollama/llamafile); a champion
    without an entry passes through untouched.
    """
    mapping = load_config().get(f"{provider}_model_map") or {}
    return str(mapping.get(model) or model)


def is_free_only_exempt(provider_name: str) -> bool:
    """Self-hosted providers have no per-token price — exempt from the
    free-only spend guardrail (DMR-052; DMR-076 adds llamafile)."""
    return provider_name in {"vllm", "ollama", "generic", "llamafile"}


def instrument_client(client) -> OpenAI:
    """Wrap the OpenAI client with the active tracing backend.

    Both Langfuse (instrumented OpenAI client) and Braintrust (wrap_openai)
    preserve the exact same `client.chat.completions.create(...)` interface, so
    agents never change. When observability is disabled, returns client as-is.
    """
    from observability.tracing import instrument_openai_client

    return instrument_openai_client(client)


def get_llm_client(agent_name: str) -> OpenAI:
    client, _ = get_llm(agent_name)
    return client


def get_llm_model(agent_name: str) -> str:
    _, model = get_llm(agent_name)
    return model
