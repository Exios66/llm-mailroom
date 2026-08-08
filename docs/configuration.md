# Configuration Reference

## `config/taxonomy.yaml`

This is the single source of truth for document classification, pipeline behavior, and agent model mappings. **Nothing is hardcoded** — adding a document class or adjusting thresholds never requires touching agent code.

### Structure

```yaml
pipeline:
  bins:                         # Filesystem paths (supports {base_dir} variable)
  confidence:                   # Thresholds for routing decisions
  doc_classes:                  # Document type definitions
  file_extensions:              # Accepted file types
  agents:                       # Per-agent model/provider configs
```

### `pipeline.bins`

Defines the filesystem layout. `{base_dir}` is resolved from the `MAILROOM_BASE_DIR` environment variable (defaults to `./data`).

```yaml
pipeline:
  bins:
    inbox: "{base_dir}/pipeline/inbox"
    processing: "{base_dir}/pipeline/processing"
    classified: "{base_dir}/pipeline/classified"
    review: "{base_dir}/pipeline/review"
    failed: "{base_dir}/pipeline/failed"
    archive: "{base_dir}/archive"
    manifests: "{base_dir}/manifests"
```

### `confidence`

Controls the branching logic in `graph/routing.py`. Tunable without code changes.

| Key | Default | Description |
|---|---|---|
| `high` | 0.85 | Above this: confident enough to proceed without second-guessing |
| `low` | 0.70 | Below this: retry once; still below after retry → human review |
| `retry_max` | 1 | Maximum retries before escalating to human review |
| `conflict_threshold` | 0.3 | Extraction confidence gap below this → potential conflict → Boss |

```yaml
confidence:
  high: 0.85
  low: 0.70
  retry_max: 1
  conflict_threshold: 0.3
```

### `doc_classes`

Each entry defines a document type. To add a new type:

1. Add an entry here
2. Create a Pydantic extraction schema in `schemas/documents.py`
3. Create a specialist agent in `agents/`
4. Register the schema in `EXTRACTION_SCHEMAS` dict in `schemas/documents.py`
5. Register the specialist dispatch in `graph/build_graph.py`

| Field | Description |
|---|---|
| `key` | Internal identifier (used in `doc_type` field) |
| `label` | Human-readable label |
| `schema` | Pydantic model name for extraction (must match `schemas/documents.py`) |
| `specialist` | Agent name (must match an entry under `agents`) |
| `description` | Used in Sorter's system prompt for classification |

```yaml
doc_classes:
  - key: contract
    label: "Contract / Agreement"
    schema: ContractExtraction
    specialist: contracts_specialist
    description: "Formal agreements between parties: M&A, vendor, employment, NDAs, etc."

  - key: corporate_record
    label: "Corporate Record"
    schema: CorporateRecordExtraction
    specialist: corporate_records_specialist
    description: "Bylaws, resolutions, board minutes, cap table entries, incorporation docs"

  - key: due_diligence
    label: "Due Diligence"
    schema: DueDiligenceExtraction
    specialist: due_diligence_specialist
    description: "Checklists, disclosure schedules, diligence memos, risk assessments"

  - key: correspondence
    label: "Correspondence"
    schema: CorrespondenceExtraction
    specialist: correspondence_specialist
    description: "Letters, emails, memos, notices between parties or with regulators"

  - key: compliance_filing
    label: "Compliance Filing"
    schema: ComplianceFilingExtraction
    specialist: compliance_specialist
    description: "SEC filings, state registrations, regulatory submissions, annual reports"
```

### `file_extensions`

Accepted file extensions for inbox processing.

```yaml
file_extensions:
  - .txt
  - .pdf
  - .docx
  - .md
```

### `agents`

Per-agent model and provider configuration. This is where agent-by-agent local model cutover happens.

| Field | Description |
|---|---|
| `provider` | LLM provider: `openrouter`, `ollama`, `vllm`, or `generic` |
| `model` | Model name (provider-specific) |
| `temperature` | LLM temperature (0.0–2.0) |
| `max_tokens` | Output token cap for the agent (bounds runaway reasoning-token generation) |

```yaml
agents:
  sorter:
    provider: openrouter
    model: openai/gpt-4o
    temperature: 0.1
    max_tokens: 2048

  contracts_specialist:
    provider: openrouter
    model: openai/gpt-4o
    temperature: 0.1
    max_tokens: 4096

  # ... (one entry per agent; includes pdf_transcriber and judge)
```

### `llm_retry`

Transient-failure retry for LLM calls (`llm/retry.py`). Retries only connection errors, timeouts, rate limits (429), and 5xx — never 4xx client errors.

| Field | Default | Description |
|---|---|---|
| `max_attempts` | 3 | Max attempts including the first |
| `base_delay` | 1.0 | Initial backoff seconds (doubles per attempt) |
| `max_delay` | 30.0 | Backoff ceiling in seconds |
| `jitter` | 0.3 | Random jitter fraction applied to each delay |

### `pipeline.pdf_direct_chars_per_page`

PDF transcription threshold. Text-based PDFs whose extraction yields at least this many chars/page are transcribed directly without an LLM pass (the dominant latency win); scanned/garbled PDFs still get the LLM reformat.

## Environment Variables

See `.env.example` for the complete list:

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | Yes (if using OpenRouter) | — | OpenRouter API key |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` | OpenRouter base URL |
| `DEFAULT_PROVIDER` | No | `openrouter` | Global provider override |
| `OLLAMA_BASE_URL` | No | `http://localhost:11434/v1` | Ollama base URL |
| `VLLM_BASE_URL` | No | `http://localhost:8000/v1` | vLLM base URL |
| `GENERIC_API_KEY` | No | — | Generic provider API key |
| `GENERIC_BASE_URL` | No | — | Generic provider base URL |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///<MAILROOM_BASE_DIR>/mailroom.db` | Async database URL. SQLite by default; set a Postgres URL to switch |
| `MAILROOM_BASE_DIR` | No | `./data` | Pipeline filesystem root (also where SQLite files live) |
| `OBSERVABILITY_PROVIDER` | No | `auto` | Tracing backend: `auto` \| `langfuse` \| `braintrust` \| `none` |
| `LOG_LEVEL` | No | `INFO` | Structured log level (`DEBUG`, `INFO`, `WARNING`, ...) |
| `LOG_FORMAT` | No | `pretty` | Log renderer: `pretty` (console) or `json` (machine-readable) |
| `LANGFUSE_PUBLIC_KEY` | No | `pk-lf-local` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | No | — | Langfuse secret key (present ⇒ `auto` picks Langfuse) |
| `LANGFUSE_HOST` | No | `http://localhost:3000` | Langfuse server URL (`LANGFUSE_BASE_URL` accepted as alias) |
| `BRAINTRUST_API_KEY` | No | — | Braintrust API key (present ⇒ `auto` picks Braintrust) |
| `BRAINTRUST_PROJECT` | No | `mailroom` | Braintrust project name |
| `MAILROOM_BASE_DIR` | No | `./data` | Pipeline filesystem root (also where SQLite files live) |
| `WATCHER_POLL_INTERVAL_SECONDS` | No | `2` | Watcher poll interval |
| `OPS_MONITOR_INTERVAL_SECONDS` | No | `300` | Ops monitor sweep interval |

## Provider Configuration

### OpenRouter (Primary)

```
OPENROUTER_API_KEY=sk-or-v1-your-key-here
DEFAULT_PROVIDER=openrouter
```

### Ollama (Local)

```bash
# Start Ollama + pull a model
docker compose -f docker/docker-compose.yml --profile local-llm up -d ollama
docker exec mailroom-ollama ollama pull qwen3:7b

# Configure
DEFAULT_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
```

### vLLM (Local)

```
DEFAULT_PROVIDER=vllm
VLLM_BASE_URL=http://localhost:8000/v1
```

### Generic OpenAI-Compatible

```
DEFAULT_PROVIDER=generic
GENERIC_BASE_URL=https://your-endpoint.com/v1
GENERIC_API_KEY=your-key
```

## Agent-by-Agent Cutover

To move individual agents to a different provider/model, edit `config/taxonomy.yaml`:

```yaml
# Before (OpenRouter):
agents:
  sorter:
    provider: openrouter
    model: openai/gpt-4o

# After (local Ollama):
agents:
  sorter:
    provider: ollama
    model: qwen3:7b
```

Or use the cutover utility:

```bash
python cutover.py --agent sorter --provider ollama --model qwen3:7b
python cutover.py --validate --agent sorter
```

See [Local Models](local-models.md) for the full cutover guide.
