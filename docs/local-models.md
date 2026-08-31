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

All agents will now use Ollama with whatever model is specified in `config/taxonomy.yaml`. The agent `model:` value is taken from the taxonomy (defaults today to `qwen/qwen3.7-flash`), never from an Ollama-side default — so with `DEFAULT_PROVIDER=ollama` run `src/scripts/cutover.py --list` first and pull a model with the same name (or move each agent per Phase 2).

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
| 6 | **Insurance Claims Specialist** | High | Coverage-determination nuance |
| 7 | **Boss** | Medium | Adjudication — lower frequency |

> The **reporter** is procedural (`get_llm("reporter")` is unused) and
> **due-diligence / court-opinion** specialists were retired in v0.5.0 — neither
> has a model to cut over.

### Using the Cutover Utility

```bash
# 1. See current assignments
PYTHONPATH=src python src/scripts/cutover.py --list

# 2. Move one agent to local
PYTHONPATH=src python src/scripts/cutover.py --agent sorter --provider ollama --model qwen3:7b

# 3. Validate with tests
PYTHONPATH=src python src/scripts/cutover.py --validate --agent sorter

# 4. If validation passes, move to the next agent
PYTHONPATH=src python src/scripts/cutover.py --agent compliance_specialist --provider ollama --model qwen3:7b
PYTHONPATH=src python src/scripts/cutover.py --validate --agent compliance_specialist

# 5. If validation fails, roll back
PYTHONPATH=src python src/scripts/cutover.py --agent sorter --provider openrouter --model openai/gpt-4o
```

### Manual Cutover (Direct YAML Edit)

Edit `config/taxonomy.yaml`:

```yaml
agents:
  sorter:
    provider: ollama            # ← changed from openrouter
    model: qwen3:7b             # ← changed from qwen/qwen3.7-flash
    temperature: 0.1
```

---

## Phase 3: Full Validation

After all agents are cut over:

```bash
# Run the full test suite
pytest -v

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

  insurance_claims_specialist:
    provider: openrouter          # Cloud for claim documentation
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

### Connection refused / provider not reachable

If the pipeline logs `APIConnectionError` or `ConnectError`:

1. Verify the service is running:
   ```bash
   # Ollama (Docker)
   docker compose -f src/config/docker/docker-compose.yml --profile local-llm ps
   curl http://localhost:11434/v1/models

   # vLLM
   curl http://localhost:8000/v1/models
   ```
2. Confirm `OLLAMA_BASE_URL` / `VLLM_BASE_URL` matches the service (defaults: `http://localhost:11434/v1`, `http://localhost:8000/v1`). Note the **`/v1` suffix is required** — the OpenAI SDK appends `/chat/completions`, so omitting it produces a 404/connection error.
3. If running Ollama **on the host** (not Docker), make sure it exposes the OpenAI-compatible endpoint: `OLLAMA_HOST=0.0.0.0 ollama serve`.
4. If agents still resolve to OpenRouter, check `DEFAULT_PROVIDER` isn't overriding: `PYTHONPATH=src python src/scripts/cutover.py --list` shows the effective provider per agent.

### HTTP 404 on `/models` or `/chat/completions`

The OpenAI SDK needs the OpenAI-compatible base URL. For Ollama that is `http://<host>:11434/v1` (the raw `:11434` root is not OpenAI-compatible). For vLLM it is `http://<host>:8000/v1`. Double-check there is no trailing slash and no extra path.

### Structured output failures

Some local models struggle with strict JSON schema mode. If you see `_parse_error: true` in extraction results:

1. Try a larger model (14B instead of 7B)
2. Try Llama 3.1 or DeepSeek-R1 for better instruction following
3. Fall back to OpenRouter for that specific agent

### JSON `json_object` mode rejected (HTTP 400)

`agents/base.py:_call_structured` deliberately embeds the literal token `json` in both the system and user messages (some providers gate `response_format: json_object` on that word). If a **local** provider still rejects the request:

1. Check whether the provider supports `response_format` at all — some local serving stacks only accept it for specific models.
2. If your local model doesn't support `json_object`, prefer a model that does (Qwen family), or route the offending agent back to OpenRouter.
3. vLLM: use an engine version that supports `guided_json`/structured output and confirm the model is served with a compatible chat template.

### Vision pages not being sent

Page images are only attached when the agent's model matches a `vision.models` substring in `taxonomy.yaml`. If your local model accepts images but pages never appear:

1. Add the model substring to `vision.models` (e.g. `"qwen"`, `"llava"`).
2. Confirm `MAILROOM_VISION_ENABLED` isn't forcing vision off.
3. Confirm `pymupdf` (fitz) is installed — it's required for PDF→image rendering (`llm/vision.py`). Without it, `_render_doc_pages` is skipped regardless of config.

### Slow inference

- Use quantized models (`qwen3:7b-q4_K_M` for GGUF quants)
- Enable GPU passthrough in Docker Compose
- Reduce context window (agents run per-agent `max_input_chars` budgets from 12K chars for the sorter/reviewer up to 100K for the contracts specialist — set yours accordingly)
- Check the model actually runs on GPU: `docker exec mailroom-ollama ollama ps` (a CPU-only model will be listed without a GPU line)

### OOM / out-of-memory

- Drop to a smaller quant (e.g. `qwen3:7b-q4_K_M` instead of `qwen3:7b` fp16)
- Reduce `num_ctx`/`num_gpu` in the Ollama model config (`ollama run --keepalive` or Modelfile)
- For vLLM, lower `--max-model-len` and `--gpu-memory-utilization` to free VRAM

### Cutover validation fails

`PYTHONPATH=src python src/scripts/cutover.py --validate --agent <name>` runs the unit tests against the new provider/model. If it fails:

1. Check the agent's `provider` and `model` values resolved correctly: `PYTHONPATH=src python src/scripts/cutover.py --list`
2. Confirm the model is pulled: `docker exec mailroom-ollama ollama list`
3. The tests never hit the real LLM — they validate the config plumbing, not the model's accuracy. For accuracy, run a pilot: `PYTHONPATH=src python src/scripts/run_pilot.py --real --source <corpus>`

### Consistent low confidence / routes to review

Smaller local models are often over-confident or under-confident. If everything lands in `review`:

1. Verify the agent model actually serves the taxonomy classes (a model not fine-tuned for legal text may classify poorly).
2. Compare against OpenRouter with `PYTHONPATH=src python src/scripts/run_vision_sweep.py --real` or a pilot diff: `PYTHONPATH=src python src/scripts/run_pilot.py --real --baseline data/pilot_report_baseline.json`.
3. Adjust `confidence.high` / `confidence.low` in `taxonomy.yaml` — thresholds are config, not code.
