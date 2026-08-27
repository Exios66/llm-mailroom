---
name: ollama
description: Local Ollama provider cutover for llm-mailroom (compose profile local-llm, DEFAULT_PROVIDER=ollama). Use for offline/local inference and qwen3:7b smoke — not the production default (prefer OpenRouter unless local was requested).
---

# Ollama (local cutover)

**When:** User asks for local/offline inference, compose `local-llm`, or
`DEFAULT_PROVIDER=ollama`.  
**Prefer OpenRouter** for production and `--real` pilots
([openrouter](../openrouter/SKILL.md)).

## Profile

| Field | Value |
| --- | --- |
| Provider | `ollama` |
| Base URL | `http://localhost:11434/v1` (`OLLAMA_BASE_URL`) |
| Default local tags | `qwen3:7b`, `llama3.2:3b` (see `llm/providers.py`) |
| Compose | `--profile local-llm` → service `mailroom-ollama` |

## Commands

```bash
docker compose -f src/config/docker/docker-compose.yml --profile local-llm up -d ollama
docker exec mailroom-ollama ollama pull qwen3:7b
# .env
DEFAULT_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
PYTHONPATH=src python src/scripts/cutover.py --all --provider ollama --model qwen3:7b
```

Rewrite models via cutover / taxonomy — never send OpenRouter ids
(`qwen/qwen3.7-flash`) to Ollama.

## When to switch away

| Need | Switch to |
| --- | --- |
| Production / cloud champion | [openrouter](../openrouter/SKILL.md) |
| Remote GPU without a local card | [modal](../modal/SKILL.md) |
| Host NVIDIA OpenAI `/v1` | `DEFAULT_PROVIDER=vllm` + local `VLLM_BASE_URL` |

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Docs: `src/config/docker/README.md`, `docs/configuration.md`
