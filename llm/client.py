from openai import OpenAI
from .providers import resolve_provider
from pipeline.config import get_agent_config


def get_llm(agent_name: str) -> tuple[OpenAI, str]:
    agent_cfg = get_agent_config(agent_name)
    provider, model = resolve_provider(agent_cfg)
    kwargs = {"base_url": provider.base_url, "api_key": "not-needed"}
    if provider.api_key_env:
        import os
        key = os.environ.get(provider.api_key_env)
        if key:
            kwargs["api_key"] = key
    client = OpenAI(**kwargs)
    return client, model


def get_llm_client(agent_name: str) -> OpenAI:
    client, _ = get_llm(agent_name)
    return client


def get_llm_model(agent_name: str) -> str:
    _, model = get_llm(agent_name)
    return model
