from openai import OpenAI
from .providers import resolve_provider
from pipeline.config import get_agent_config
from pipeline.env import load_env

load_env()


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
    client = instrument_client(client)
    return client, model


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
