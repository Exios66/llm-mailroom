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
