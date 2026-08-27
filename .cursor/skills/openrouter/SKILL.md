---
name: openrouter
description: Production default LLM path for llm-mailroom (OpenRouter via get_llm). Use when changing providers, taxonomy models, cost_models, cutover, OPENROUTER_API_KEY, or debugging generations — prefer over Ollama/Modal unless the user asked for local or GPU serve.
---

# OpenRouter (production default)

**When:** Live pipeline, `--real` pilots, taxonomy model registry, cost prices,
unexpected generations.  
**Prefer over:** Ollama and Modal unless the user requested local/offline or
remote GPU.

## Contract

| Knob | Value |
| --- | --- |
| Env | `DEFAULT_PROVIDER=openrouter` (default) |
| Key | `OPENROUTER_API_KEY` — required or `llm/client.py:get_llm` raises |
| Base | `https://openrouter.ai/api/v1` |
| Access | `get_llm(agent_name)` only; `agent_name` must be a key under `agents:` in `taxonomy.yaml` |
| Champion | `qwen/qwen3.7-flash` (see `cost_models:`) |
| Completions | always through `llm/retry.py:retry_chat_completion` |

Never name a provider or model inside agent code. Global override:
`DEFAULT_PROVIDER`. Per-agent: `taxonomy.yaml` then `scripts/cutover.py`.

## Commands

```bash
PYTHONPATH=src python src/scripts/cutover.py --list
PYTHONPATH=src python src/scripts/cutover.py --recommend
PYTHONPATH=src python src/scripts/cutover.py --validate --agent sorter
```

## Cost gotchas

- Langfuse generation cost is computed at ingestion from **`cost_details`**.
- Register the model in `taxonomy.yaml` `cost_models:` *before* first use
  (`scripts/sync_models.py`); Langfuse negatively caches unknown models 24h.

## Depth (vendored)

For catalog/pricing/latency, generations, spend, and benchmarks use
`.opencode/skills/openrouter-models` (and `openrouter-generations` /
`openrouter-analytics` / `openrouter-benchmarks`).

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Local: [ollama](../ollama/SKILL.md) · GPU: [modal](../modal/SKILL.md)
