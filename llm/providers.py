from typing import Dict, Optional
import os
from dataclasses import dataclass, field

DEFAULT_MODELS = {
    "openrouter": [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "anthropic/claude-sonnet-4-20250514",
        "anthropic/claude-3.5-sonnet",
        "google/gemini-2.0-flash",
        "google/gemini-2.5-pro",
        "meta-llama/llama-4-maverick",
        "deepseek/deepseek-chat",
        "qwen/qwen-3-7b",
    ],
    "ollama": [
        "qwen3:7b",
        "qwen3:14b",
        "qwen2.5:14b",
        "qwen2.5:32b",
        "llama3.1:8b",
        "llama3.1:70b",
        "llama3.2:3b",
        "mistral:7b",
        "mistral-nemo:12b",
        "mixtral:8x7b",
        "deepseek-r1:8b",
        "deepseek-r1:14b",
        "phi4:14b",
        "gemma2:9b",
        "gemma2:27b",
        "command-r:35b",
        "command-r-plus:104b",
        "nous-hermes2:10.7b",
        "dolphin-mixtral:8x7b",
        "wizardlm2:8x7b",
    ],
    "vllm": [
        "*",
    ],
    "generic": [
        "*",
    ],
}


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str | None
    default_model: str
    available_models: list[str] = field(default_factory=list)


def _build_providers() -> Dict[str, ProviderConfig]:
    return {
        "openrouter": ProviderConfig(
            name="openrouter",
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key_env="OPENROUTER_API_KEY",
            default_model="openai/gpt-4o",
            available_models=DEFAULT_MODELS["openrouter"],
        ),
        "ollama": ProviderConfig(
            name="ollama",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key_env=None,
            default_model="qwen3:7b",
            available_models=DEFAULT_MODELS["ollama"],
        ),
        "vllm": ProviderConfig(
            name="vllm",
            base_url=os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"),
            api_key_env=None,
            default_model="*",
            available_models=DEFAULT_MODELS["vllm"],
        ),
        "generic": ProviderConfig(
            name="generic",
            base_url=os.environ.get("GENERIC_BASE_URL", ""),
            api_key_env="GENERIC_API_KEY",
            default_model="*",
            available_models=DEFAULT_MODELS["generic"],
        ),
    }


_providers_cache: Optional[Dict[str, ProviderConfig]] = None


def get_providers() -> Dict[str, ProviderConfig]:
    global _providers_cache
    if _providers_cache is None:
        _providers_cache = _build_providers()
    return _providers_cache


def get_provider(name: str) -> ProviderConfig:
    providers = get_providers()
    if name not in providers:
        raise ValueError(f"Unknown provider: {name}. Available: {list(providers.keys())}")
    return providers[name]


def resolve_provider(agent_config: dict) -> tuple[ProviderConfig, str]:
    provider_name = os.environ.get("DEFAULT_PROVIDER") or agent_config.get("provider", "openrouter")
    model = agent_config.get("model", "openai/gpt-4o")
    provider = get_provider(provider_name)

    if provider_name == "openrouter" and provider.api_key_env:
        key = os.environ.get(provider.api_key_env, "")
        if not key:
            raise ValueError(
                f"OpenRouter API key not set. Set {provider.api_key_env} env var."
            )

    if not provider.base_url:
        raise ValueError(f"No base URL configured for provider '{provider_name}'.")

    return provider, model
