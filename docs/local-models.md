# Local Model Cutover Guide

Mailroom is designed for provider-agnostic LLM usage. OpenRouter is the primary provider today, but switching to local models (Ollama, vLLM) is a configuration change — no code rewrite required.

---

## Architecture

The LLM layer is abstracted in two files:

```
llm/
├── client.py       # get_llm(agent_name) → OpenAI client
└── providers.py    # Provider configs: base URLs, models, auth
```

And one config file:

```
config/
└── taxonomy.yaml   # agents: section — per-agent provider + model
```

Provider selection flow:
```
taxonomy.yaml (agent config)
    → llm/client.py (resolve agent name)
        → llm/providers.py (resolve provider config)
            → openai.OpenAI(base_url=..., api_key=...)
```

---

## Available Local Models

### Recommended Primary Local Model: Qwen 3

**Qwen 3 7B** (`qwen3:7b`) is the recommended primary local model for Mailroom:
- Strong structured JSON output (critical for extraction schemas)
- Good legal text understanding
- Available 14B variant for higher accuracy
- Part of the same Qwen family as OpenRouter's `qwen/qwen-3-7b`

### Full Local Model Catalog (Ollama)

| Model Family | Available Sizes | Strengths | Weaknesses |
|---|---|---|---|
| **Qwen 3** | 7b, 14b | Structured output, legal text | Medium resource usage |
| **Qwen 2.5** | 14b, 32b | Multilingual, strong extraction | Larger size |
| **Llama 3.1** | 8b, 70b | Reliable all-around | Weaker structured output |
| **Llama 3.2** | 3b | Very fast, lightweight | Limited complex extraction |
| **Mistral** | 7b | Fast, good instructions | Less legal domain knowledge |
| **Mistral Nemo** | 12b | Good speed/quality balance | — |
| **Mixtral** | 8x7b | MoE — strong extraction | Higher memory usage |
| **DeepSeek-R1** | 8b, 14b | Legal reasoning, analysis | Slower inference |
| **Phi-4** | 14b | Document understanding | — |
| **Gemma 2** | 9b, 27b | Instruction following | — |
| **Command R** | 35b, 104b | RAG, extraction | Very high resource usage |

---

## Phase 1: Global Cutover (Fastest)

Set a single environment variable to switch ALL agents to local:

```bash
export DEFAULT_PROVIDER=ollama
```

All agents will now use Ollama with whatever model is specified in `config/taxonomy.yaml`. If you haven't changed the per-agent models, they'll default to `qwen3:7b` (the Ollama default).

---

## Phase 2: Agent-by-Agent Cutover (Recommended)

Move agents one at a time, validating each before moving the next. This minimizes risk.

### Recommended Cutover Order (Least Risky First)

| Order | Agent | Risk | Rationale |
|---|---|---|---|
| 1 | **Sorter** | Low | Classification is the least accuracy-sensitive |
| 2 | **Compliance Specialist** | Low | Structured forms, predictable formats |
| 3 | **Correspondence Specialist** | Medium | Narrative text, moderate complexity |
| 4 | **Corporate Records Specialist** | Medium | Hierarchical data, moderate complexity |
| 5 | **Contracts Specialist** | Medium-High | Complex extraction, legal precision |
| 6 | **Due Diligence Specialist** | High | Risk detection nuance |
| 7 | **Reporter** | Medium | Summarization — lower stakes |
| 8 | **Boss** | Medium | Adjudication — lower frequency |

### Using the Cutover Utility

```bash
# 1. See current assignments
python cutover.py --list

# 2. Move one agent to local
python cutover.py --agent sorter --provider ollama --model qwen3:7b

# 3. Validate with tests
python cutover.py --validate --agent sorter

# 4. If validation passes, move to the next agent
python cutover.py --agent compliance_specialist --provider ollama --model qwen3:7b
python cutover.py --validate --agent compliance_specialist

# 5. If validation fails, roll back
python cutover.py --agent sorter --provider openrouter --model openai/gpt-4o
```

### Manual Cutover (Direct YAML Edit)

Edit `config/taxonomy.yaml`:

```yaml
agents:
  sorter:
    provider: ollama          # ← changed from openrouter
    model: qwen3:7b           # ← changed from openai/gpt-4o
    temperature: 0.1
```

---

## Phase 3: Full Validation

After all agents are cut over:

```bash
# Run the full test suite
pytest tests/ -v

# Compare extraction accuracy with golden fixtures
# This requires OpenRouter to still be available for comparison:
python -c "
# Run each fixture through both providers and compare extraction outputs
"
```

---

## Provider Comparison Table

| Capability | OpenRouter (GPT-4o) | Ollama (Qwen 3 7B) | Ollama (Llama 3.1 8B) |
|---|---|---|---|
| Structured JSON output | Excellent | Very Good | Good |
| Legal terminology | Excellent | Good | Fair |
| Instruction following | Excellent | Good | Very Good |
| Inference speed | Depends on provider | Fast (local GPU) | Fast (local GPU) |
| Cost per document | ~$0.01-0.05 | $0 (local) | $0 (local) |
| Data privacy | Documents leave infra | Documents stay local | Documents stay local |
| Availability | Requires internet | Fully offline | Fully offline |

---

## Hybrid Mode

You can run a mix of providers simultaneously. For example:

```yaml
agents:
  sorter:
    provider: ollama              # Fast local classification
    model: qwen3:7b

  contracts_specialist:
    provider: openrouter          # Cloud for complex contracts
    model: openai/gpt-4o

  compliance_specialist:
    provider: ollama              # Local for structured filings
    model: qwen3:7b

  due_diligence_specialist:
    provider: openrouter          # Cloud for risk detection
    model: openai/gpt-4o
```

This gives you cost savings on simpler tasks while retaining accuracy on complex ones.

---

## Hardware Requirements

| Model Size | Min RAM | Recommended RAM | Min VRAM (GPU) |
|---|---|---|---|
| 7B/8B | 8 GB | 16 GB | 6 GB |
| 12B-14B | 16 GB | 32 GB | 10 GB |
| 32B-35B | 32 GB | 64 GB | 24 GB |
| 70B+ | 64 GB | 128 GB | 48 GB |

For pilot scale (dozens of documents/day), a machine with 16GB RAM and a GPU with 8GB+ VRAM running qwen3:7b is sufficient.

---

## Troubleshooting Local Models

### Model not found

```bash
# List available models
docker exec mailroom-ollama ollama list

# Pull a model
docker exec mailroom-ollama ollama pull qwen3:7b
```

### Structured output failures

Some local models struggle with strict JSON schema mode. If you see `_parse_error: true` in extraction results:

1. Try a larger model (14B instead of 7B)
2. Try Llama 3.1 or DeepSeek-R1 for better instruction following
3. Fall back to OpenRouter for that specific agent

### Slow inference

- Use quantized models (`qwen3:7b-q4_K_M` for GGUF quants)
- Enable GPU passthrough in Docker Compose
- Reduce context window (agents truncate to 12K-25K chars already)
