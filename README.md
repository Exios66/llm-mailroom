# Mailroom — Multi-Agent Legal Document Processing Pipeline

## Quick Start

```bash
# 1. Start infrastructure
docker compose -f docker/docker-compose.yml up -d postgres clickhouse langfuse-server

# 2. Configure
cp .env.example .env
# Edit .env — add your OPENROUTER_API_KEY

# 3. Install
pip install -e ".[dev]"

# 4. Run the watcher (starts processing documents from inbox)
python pipeline/watcher.py

# 5. Upload a document
curl -X POST http://localhost:8000/upload \
  -F "file=@sample_contract.txt" \
  -F "matter_id=MATTER-001"

# 6. Check status
curl http://localhost:8000/status/{doc_id}
```

## Architecture

```
Upload/Drop → /pipeline/inbox/ → [Watcher] → LangGraph run per document
                                                  │
                    Sorter → Specialist → Reporter → Catalog → Archivist
                                                  │
                    Boss (escalation)    Human Review    Audit Log
```

11 LangGraph nodes in a Postgres-checkpointed state machine.

## Config

`config/taxonomy.yaml` — doc classes, confidence thresholds, per-agent model mappings. Adding a document class or adjusting thresholds never requires touching agent code.

## Providers

| Provider | Config | Auth |
|---|---|---|
| OpenRouter (primary) | `.env` → `OPENROUTER_API_KEY` | API key |
| Ollama (local) | `.env` → `OLLAMA_BASE_URL` | None |
| vLLM (local) | `.env` → `VLLM_BASE_URL` | None |
| Generic OpenAI-compatible | `.env` → `GENERIC_BASE_URL` + `GENERIC_API_KEY` | Optional key |

Global override: set `DEFAULT_PROVIDER=ollama` in `.env`.

## Agent-by-Agent Local Cutover

```bash
# See current assignments
python cutover.py --list

# Move sorter to local (safest first step)
python cutover.py --agent sorter --provider ollama --model qwen3:7b

# Validate
python cutover.py --validate --agent sorter

# Move all agents to local
python cutover.py --all --provider ollama --model qwen3:7b

# View recommended cutover order
python cutover.py --recommend
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/upload` | Upload document to inbox |
| `POST` | `/review/{doc_id}/resolve` | Resolve human review (approved/rejected) |
| `GET` | `/status/{doc_id}` | Document pipeline status |
| `GET` | `/matters/{matter_id}` | All documents in a matter |
| `GET` | `/audit/{doc_id}` | Hash-chained audit trail + validity check |
| `GET` | `/ops/status` | Pipeline-wide operational metrics |
| `GET` | `/health` | Health check |

## Testing

```bash
pytest tests/ -v
```

## Available Local Models (Ollama)

| Model Family | Available Sizes | Best For |
|---|---|---|
| Qwen 3 | 7b, 14b | Structured output, legal text extraction |
| Qwen 2.5 | 14b, 32b | Strong multilingual support |
| Llama 3.1 | 8b, 70b | General-purpose, reliable structured output |
| Llama 3.2 | 3b | Lightweight, fast classification |
| Mistral | 7b | Fast instruction following |
| Mistral Nemo | 12b | Good balance of speed and quality |
| Mixtral | 8x7b | MoE model, strong extraction |
| DeepSeek-R1 | 8b, 14b | Reasoning, legal analysis |
| Phi-4 | 14b | Document understanding |
| Gemma 2 | 9b, 27b | Instruction following |
| Command R | 35b, 104b | RAG, extraction |
