import os

from openai import OpenAI
from .providers import resolve_provider
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


def get_llm(agent_name: str) -> tuple[OpenAI, str]:
    agent_cfg = get_agent_config(agent_name)
    provider, model = resolve_provider(agent_cfg)
    if provider.name in _SELF_HOSTED_PROVIDERS:
        # DMR-052/076: the served model id differs from the taxonomy's
        # OpenRouter champion slug — remap before the client exists so the
        # endpoint never 404s on the champion id.
        model = _self_hosted_model(model, provider.name)
    # The free-only guardrail bounds OpenRouter spend; self-hosted providers
    # (vLLM/ollama/llamafile/generic) have no per-token price and are exempt
    # (DMR-052).
    if not is_free_only_exempt(provider.name):
        assert_free_model(model)
        if (
            free_only_enabled()
            and provider.api_key_env
            and provider.api_key_env != "OPENROUTER_API_KEY"
        ):
            raise RuntimeError(
                f"MAILROOM_LLM_FREE_ONLY is on: agent '{agent_name}' resolves "
                f"provider credential '{provider.api_key_env}' outside the "
                "OpenRouter free tier — refusing."
            )
    kwargs = {"base_url": provider.base_url, "api_key": "not-needed"}
    if provider.api_key_env:
        key = os.environ.get(provider.api_key_env)
        if key:
            kwargs["api_key"] = key
    client = OpenAI(**kwargs)
    client = instrument_client(client)
    return client, model


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
