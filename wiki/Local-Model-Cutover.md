# Local Model Cutover

Mailroom is provider-agnostic. Switching from OpenRouter to local models (Ollama/vLLM) is a configuration change, not a code rewrite.

---

## Provider Selection Flow

```
config/taxonomy.yaml (per-agent provider + model)
    --> llm/client.py (resolve)
        --> llm/providers.py (provider config)
            --> openai.OpenAI(base_url=..., api_key=...)
```

---

## Available Local Models

### Recommended Primary: Qwen 3 7B

Strong structured JSON output (critical for extraction schemas), good legal text understanding.

### Full Catalog (Ollama)

| Model | Sizes | Best For |
|---|---|---|
| Qwen 3 | 7b, 14b | Structured output, legal text |
| Qwen 2.5 | 14b, 32b | Multilingual |
| Llama 3.1 | 8b, 70b | General-purpose |
| Llama 3.2 | 3b | Fast classification |
| Mistral | 7b | Fast instructions |
| Mistral Nemo | 12b | Speed/quality balance |
| Mixtral | 8x7b | Extraction (MoE) |
| DeepSeek-R1 | 8b, 14b | Legal reasoning |
| Phi-4 | 14b | Document understanding |
| Gemma 2 | 9b, 27b | Instructions |
| Command R | 35b, 104b | RAG, extraction |

---

## Cutover Methods

### Global (fastest)

```bash
export DEFAULT_PROVIDER=ollama
```

All agents use Ollama with their configured model.

### Per-Agent (recommended)

Use the cutover utility:

```bash
python cutover.py --list                          # See current assignments
python cutover.py --agent sorter --provider ollama --model qwen3:7b
python cutover.py --validate --agent sorter       # Run tests
python cutover.py --recommend                     # View cutover order
```

Or edit `config/taxonomy.yaml` directly:

```yaml
agents:
  sorter:
    provider: ollama
    model: qwen3:7b
```

### Recommended Cutover Order

1. Sorter (lowest risk — classification)
2. Compliance Specialist (structured forms)
3. Correspondence Specialist (narrative text)
4. Corporate Records Specialist (hierarchical data)
5. Contracts Specialist (complex extraction)
6. Due Diligence Specialist (risk detection nuance)
7. Reporter (summarization)
8. Boss (adjudication)

---

## Hybrid Mode

Run a mix of local and cloud models:

```yaml
agents:
  sorter:
    provider: ollama
    model: qwen3:7b
  due_diligence_specialist:
    provider: openrouter
    model: openai/gpt-4o
```

---

## Hardware Requirements

| Model Size | Min RAM | Recommended | Min VRAM |
|---|---|---|---|
| 7B/8B | 8 GB | 16 GB | 6 GB |
| 12B-14B | 16 GB | 32 GB | 10 GB |
| 32B-35B | 32 GB | 64 GB | 24 GB |
| 70B+ | 64 GB | 128 GB | 48 GB |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| Model not found | `docker exec mailroom-ollama ollama pull qwen3:7b` |
| JSON parse errors | Try larger model or fall back to OpenRouter for that agent |
| Slow inference | Use quantized models, enable GPU, reduce context window |
| Connection refused | Verify service is up (`curl http://localhost:11434/v1/models`); confirm `OLLAMA_BASE_URL`/`VLLM_BASE_URL` includes the `/v1` suffix |
| HTTP 404 on `/models` | Base URL must be OpenAI-compatible (`:11434/v1`, `:8000/v1`) — the raw root is not |
| JSON mode rejected (HTTP 400) | Use a model that supports `response_format: json_object` (Qwen family) or route the agent to OpenRouter |
| Vision pages missing | Add the model substring to `vision.models` in `taxonomy.yaml`; confirm `MAILROOM_VISION_ENABLED` and that `pymupdf` (fitz) is installed |
| OOM | Use a smaller quant (`qwen3:7b-q4_K_M`); reduce `num_ctx`/GPU memory in vLLM |
| Cutover validation fails | Check `cutover.py --list`; confirm model is pulled; tests validate config plumbing, run a `--real` pilot for accuracy |
| Everything routes to review | Compare against OpenRouter with a pilot baseline; tune `confidence.high`/`confidence.low` in `taxonomy.yaml` |

See `docs/local-models.md` for the full guide.
