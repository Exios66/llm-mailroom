# `llm/` — How the pipeline talks to AI models

## What this folder is (plain English)

This is the **translator** between Mailroom and whatever AI company or local model you're using. The pipeline calls one function — `get_llm(agent_name)` — and gets back a ready-to-use chat client plus the model name. It doesn't care whether that's OpenRouter (cloud), Ollama (local), or any OpenAI-compatible endpoint.

Where the model comes from: `config/taxonomy.yaml` → `agents:` section → this folder resolves provider + model → returns `(client, model)`.

**Rule of the codebase:** no agent code ever names a provider or model. Everything goes through this folder, and you change models in config, not code.

## Providers

| Provider | Default base URL | Auth env var |
|---|---|---|
| `openrouter` (default) | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | — |
| `vllm` | `http://localhost:8000/v1` | — |
| `generic` | `GENERIC_BASE_URL` | `GENERIC_API_KEY` |

Override the provider for **all** agents at once with the `DEFAULT_PROVIDER` env var.

## Technical reference

- `client.py`
  - `get_llm(agent_name) -> (OpenAI, model)` — reads the agent config, resolves provider/model, returns an OpenAI-compatible client.
  - `get_llm_client(agent_name)`, `get_llm_model(agent_name)` — convenience accessors.
  - Raises `ValueError` if `OPENROUTER_API_KEY` is missing (only when provider is OpenRouter).
- `providers.py`
  - `ProviderConfig` dataclass: `name`, `base_url`, `api_key_env`, `default_model`, `available_models`.
  - `resolve_provider(agent_config) -> (ProviderConfig, model)` — applies the `DEFAULT_PROVIDER` override, then falls back to the agent's own `provider`/`model`.
  - `DEFAULT_MODELS` — the menu of known model names per provider (used by `scripts/cutover.py` recommendations and validation).
- `get_llm` always builds an `OpenAI` client (OpenAI-compatible protocol), so all providers speak the same API.
- Local cutover tooling: `scripts/cutover.py` edits `config/taxonomy.yaml` agent entries; `docs/local-models.md` is the walkthrough.
