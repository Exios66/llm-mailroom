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
| `high` | 0.97 (global fallback) | Classification/extraction confidence ≥ this auto-continues; per-class overrides apply after `doc_type` is known |
| `low` | 0.88 (global fallback) | Below this: retry while `attempts <= retry_max`, then human review |
| `retry_max` | 2 | Maximum classify/extract retries before escalating to human review |
| `judge_band_high` | 0.95 (global) | Lane B completeness-judge gate: `low <= extraction_confidence < judge_band_high` fires `judge_verify` |
| `arbiter_retry_max` | 2 | Max arbiter-approved re-extract loops (approval-inclusive) |
| `judge_max_passes` | 3 | `1 + arbiter_retry_max` — one completeness judge per extraction attempt |
| `conflict_threshold` | 0.3 | **Unused routing knob** (kept for config-file compatibility). Matter conflicts escalate via deterministic same-class field comparison in `graph/build_graph.py:_detect_conflict`, not an extraction-confidence gap. |
| `by_class` | (severity map) | Per-class `high` / `low` / `judge_band_high` for critical contracts/mergers/insurance, high compliance, elevated corporate, standard correspondence |

```yaml
confidence:
  high: 0.97
  low: 0.88
  retry_max: 2
  judge_band_high: 0.95
  arbiter_retry_max: 2
  judge_max_passes: 3
  conflict_threshold: 0.3  # unused; conflicts are field-value comparison
  by_class:
    contract: { severity: critical, high: 0.98, low: 0.90, judge_band_high: 0.97 }
    # … merger_agreement, insurance_claim, compliance_filing, corporate_record, correspondence
```

### `doc_classes`

Each entry defines a document type. To add a new type:

1. Add an entry here
2. Create a Pydantic extraction schema in `schemas/documents.py`
3. Create a specialist agent in `agents/`
4. Register the schema in `EXTRACTION_SCHEMAS` dict in `schemas/documents.py`
5. Register the specialist dispatch in `graph/build_graph.py`

| Field | Description |
|---|---|---|
| `key` | Internal identifier (used in `doc_type` field) |
| `label` | Human-readable label |
| `schema` | Pydantic model name for extraction (must match `schemas/documents.py`) |
| `specialist` | Agent name (must match an entry under `agents`) |
| `description` | Used in Sorter's system prompt for classification |
| `field_types` | Per-field deterministic scoring type for the extraction schema (see `field_scoring` below) |

```yaml
doc_classes:
  - key: contract
    label: "Contract / Agreement"
    schema: ContractExtraction
    specialist: contracts_specialist
    description: "Formal agreements between parties: M&A, vendor, employment, NDAs, etc."
    field_types:
      parties: entity_list:name
      effective_date: date
      contract_value: money

  - key: corporate_record
    label: "Corporate Record"
    schema: CorporateRecordExtraction
    specialist: corporate_records_specialist
    description: "Bylaws, resolutions, board minutes, cap table entries, incorporation docs"

  - key: correspondence
    label: "Correspondence"
    schema: CorrespondenceExtraction
    specialist: correspondence_specialist
    description: "Letters, emails, memos, notices between parties or with regulators"
    field_types:
      sender: name
      recipient: name
      additional_recipients: entity_list
      communication_type: name
      communication_date: date
      key_points: entity_list
      demand_amount: money
      action_items: entity_list
      urgency: name
      referenced_communications: entity_list

  - key: compliance_filing
    label: "Compliance Filing"
    schema: ComplianceFilingExtraction
    specialist: compliance_specialist
    description: "SEC filings, state registrations, regulatory submissions, annual reports"

  - key: insurance_claim
    label: "Insurance Claim"
    schema: InsuranceClaimExtraction
    specialist: insurance_claims_specialist
    description: "FNOL forms, adjuster reports, demand packages, coverage determinations, denial letters"
```

### `field_scoring`

Controls the deterministic field-type-aware extraction scorer (`observability/field_scoring.py`). Each field is normalized by its type before comparison: `id`/`date`/`money` are normalized then exact-matched, `name` uses Jaro-Winkler + token-set ratio, `free_text` uses token F1, and `entity_list` uses optimal bipartite matching (Hungarian) with precision/recall/F1. Embedding cosine similarity (sentence-transformers) acts as a second signal for ambiguous name/free-text fields.

| Key | Default | Description |
|---|---|---|
| `ambiguous_band` | `[0.5, 0.85]` | Global scores-inside-this-band → escalate to the LLM judge (fallback when no per-type band applies) |
| `type_bands` | see YAML | **Per-field-type** judge-escalation bands, calibrated by `scripts/calibrate_field_scoring.py` (issues #4/#5). `date`/`id` are `never` (deterministic score decisive both ways); `money`/`free_text` get calibrated numeric cutoffs; `name`/`entity_list` use `[0.5, 1.0]` (trust only perfect scores, escalate near-misses — Jaro-Winkler/token-set are typo-tolerant by design, so a deterministic reject is unreliable). `always`/`never` are also accepted. |
| `bipartite_match_threshold` | `0.6` | Minimum pairwise similarity for an entity-list match |
| `embedding_enabled` | `true` | Use embedding cosine rescue for ambiguous name/free-text fields |
| `embedding_model` | `sentence-transformers/all-MiniLM-L6-v2` | Sentence-transformer model for the embedding signal |
| `embedding_rescue_below` | `0.7` | Only consult embeddings when the string score is below this |

```yaml
field_scoring:
  ambiguous_band: [0.5, 0.85]
  type_bands:
    date: never              # exact-after-normalize: decisive both ways
    id: never                # exact match: decisive both ways
    money: [0.675, 0.938]    # numeric tolerance: calibrated cutoff
    free_text: [0.6, 0.95]   # token F1: paraphrases legitimately score 0.6-0.88 — escalate them
    name: [0.5, 1.0]         # trust only perfect matches; near-misses go to judge
    entity_list: [0.5, 1.0]  # trust only perfect lists; partial matches go to judge
  bipartite_match_threshold: 0.6
  embedding_enabled: true
  embedding_model: sentence-transformers/all-MiniLM-L6-v2
  embedding_rescue_below: 0.7
```

### `vision`

Controls vision-mode ingestion — rendering PDF/scan pages to image data-URIs for vision-capable models. Vision is **additive**: the full `doc_text` transcription is always sent; page images are appended only when the input agent's model matches one of `vision.models` (substring match). `max_pages` bounds the image budget, never the content.

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch for vision ingestion |
| `max_pages` | `10` | Max PDF pages rendered as images per document (0 = all pages) |
| `dpi` | `150` | Render resolution for page images |
| `models` | — | Model strings (substring match) that accept image input (`qwen/`, `gpt-4o`, `claude`, `gemini`, …) |

```yaml
vision:
  enabled: true
  max_pages: 10
  dpi: 150
  models:
    - "qwen/"
    - "gpt-4o"
    - "gpt-4.1"
    - "claude"
    - "gemini"
    - "llava"
    - "llama-3.2"
    - "qwen-vl"
```

**Environment overrides** (useful for pilot sweeps without editing YAML): `MAILROOM_VISION_ENABLED`, `MAILROOM_VISION_MAX_PAGES`, `MAILROOM_VISION_DPI`.

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
| `max_input_chars` | Input-text budget per call (defaults to 25000); large docs need a bigger window or late-page fields get truncated |
| `reasoning_effort` | OpenRouter reasoning budget for thinking models: `none`, `low`, `medium`, `high`, `max` |

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
| `VLLM_API_KEY` | No | — | Optional bearer token for the vLLM endpoint (keyless local servers work — the client falls back internally) |
| `GENERIC_API_KEY` | No | — | Generic provider API key |
| `GENERIC_BASE_URL` | No | — | Generic provider base URL |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///<MAILROOM_BASE_DIR>/mailroom.db` | Async database URL. SQLite by default; set a Postgres URL to switch |
| `MAILROOM_BASE_DIR` | No | `./data` | Pipeline filesystem root (also where SQLite + `warehouse/` Parquet files live) |
| `MAILROOM_WAREHOUSE_EXPORT` | No | `auto` | After `archived`/`failed`, append rows to `data/warehouse/documents_YYYY-MM-DD.parquet` + matching audit file. `auto` = on when pyarrow is installed; `1`/`0` to force. Backfill: `scripts/export_warehouse.py`. |
| `OBSERVABILITY_PROVIDER` | No | `auto` | Tracing backend: `auto` \| `langfuse` \| `braintrust` \| `phoenix` \| `none`. `auto` = Langfuse if key → Braintrust if key → local Arize Phoenix (cost-free) → `none` |
| `OBSERVABILITY_ENVIRONMENT` | No | entrypoint default (`live`/`pilot`/`misc`/`mock`) | Environment label copied onto every Langfuse observation |
| `LANGFUSE_PUBLIC_KEY` | No | `pk-lf-local` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | No | — | Langfuse secret key (present ⇒ `auto` picks Langfuse) |
| `LANGFUSE_HOST` | No | `http://localhost:3000` | Langfuse server URL (`LANGFUSE_BASE_URL` accepted as alias) |
| `LANGFUSE_FLUSH_AT` | No | SDK default `512` | Max queued events before the background exporter sends a batch. Do not set to `1` globally (API stampede). Short-lived jobs still MUST `flush()` before exit |
| `LANGFUSE_FLUSH_INTERVAL` | No | SDK default `5` (seconds) | Max seconds the background exporter waits before sending a batch |
| `LANGFUSE_RELEASE` | No | `mailroom@<pkg version>` | Release/version label on every observation |
| `LANGFUSE_TIMEOUT` | No | SDK default | HTTP timeout (seconds) for the Langfuse client |
| `LANGFUSE_SAMPLE_RATE` | No | `1.0` | Trace sampling rate (`0`–`1`) |
| `MAILROOM_TRACE_USER_ID` | No | — | Optional Langfuse `user_id` propagated onto every `document-pipeline` trace |
| `LOG_LEVEL` | No | `INFO` | Structured log level (`DEBUG`, `INFO`, `WARNING`, ...) |
| `LOG_FORMAT` | No | `pretty` | Log renderer: `pretty` (console) or `json` (machine-readable) |
| `LOG_FILE` | No | — | Optional rotating file sink: every structlog event is appended here as a JSON line (audit item 10.3 — no unbounded log files) |
| `LOG_MAX_BYTES` | No | `10485760` | Rotation size per log file (10 MB default) |
| `LOG_BACKUP_COUNT` | No | `5` | Rotated log files kept |
| `BRAINTRUST_API_KEY` | No | — | Braintrust API key (present ⇒ `auto` picks Braintrust) |
| `BRAINTRUST_PROJECT` | No | `mailroom` | Braintrust project name |
| `PHOENIX_TRACING` | No | `enabled` | Enable the local Arize Phoenix OTel backend (default fallback in `auto`) |
| `PHOENIX_ENDPOINT` | No | `http://localhost:6006/v1/traces` | Phoenix OTLP HTTP endpoint |
| `PHOENIX_SERVICE_NAME` | No | `mailroom` | OTel service name |
| `PHOENIX_PROJECT` | No | `mailroom` | Phoenix openinference project name |
| `MAILROOM_CHECKPOINTER` | No | `memory` | LangGraph checkpointer backend: `memory` (MemorySaver default — process-level compiled graph so `interrupt()` HITL can resume in-process; review bin is the durable park, with extract-invoke fallback if the checkpoint is gone) or `sqlite` (on-disk SqliteSaver at `<MAILROOM_BASE_DIR>/checkpoints.db`, for debugging/resume-across-restart experiments) |
| `MAILROOM_JUDGE_VERIFY` | No | `on` | Kill-switch for the judge-verify exception lane (KANBAN-063): set `off`/`false`/`0`/`no` to skip gated completeness verification entirely. When on, the lane fires only on grounded extractions landing in the ambiguous band (`low <= confidence < judge_band_high`, default 0.85) — clean high-confidence extractions cost zero added judge calls |
| `MAILROOM_BASE_DIR` | No | `./data` | Pipeline filesystem root (also where SQLite files live) |
| `WATCHER_POLL_INTERVAL_SECONDS` | No | `1` | Inbox rescan interval (seconds) |
| `MAILROOM_EMBED_WATCHER` | No | on (off under pytest) | API lifespan starts the inbox watcher. Set `0` when a dedicated `python -m pipeline.watcher` holds `watcher.lock` |
| `MAILROOM_API_TOKEN` | Off-loopback yes | — | Bearer token for every route except `/health`. The-Mailroom `MAILROOM_PIPELINE_TOKEN` must match. |
| `MAILROOM_API_HOST` | No | `127.0.0.1` | Bind address. `0.0.0.0` requires a live token. Container/Space images set this. |
| `MAILROOM_API_PORT` | No | `8000` (image: `7860`) | Listen port. Falls back to `PORT` for Spaces/Fly/Render. |

The-Mailroom (not this process) reads `MAILROOM_PIPELINE_URL`,
`MAILROOM_PIPELINE_TOKEN`, and `MAILROOM_PIPELINE_API_PREFIX=/v1`. A Space
Observatory must use the public producer Space URL — see
[`deploy/space/PAIRING.md`](../deploy/space/PAIRING.md).
| `WATCHER_STALE_SECONDS` | No | `15` | `/health` `checks.watcher` lamp: heartbeat older than this is `stale` |
| `OPS_MONITOR_INTERVAL_SECONDS` | No | `300` | Ops monitor sweep interval |
| `MAILROOM_VISION_ENABLED` | No | `true` | Enable/disable vision ingestion (overrides `vision.enabled` in taxonomy.yaml) |
| `MAILROOM_VISION_MAX_PAGES` | No | `10` | Max PDF pages to render as images (0 = all pages; overrides `vision.max_pages`) |
| `MAILROOM_VISION_DPI` | No | `150` | Render DPI for page images (overrides `vision.dpi`) |
| `MAILROOM_PILOT_COST_ABORT` | No | `2.00` (HF pilot) / `0.20` (committed-sample `run_pilot.py`) | Cumulative USD cap; abort the pilot when exceeded |
| `MAILROOM_DOCCLASS_PROMPTS` | No | off | Opt-in KANBAN-090 docclass prompt arm (`1`/`true`/`yes`/`on`). Runtime fetches `mailroom-docclass-<key>` with the in-repo append as fallback; production `mailroom-<agent>` templates are unchanged. `run_hf_pilot.py --docclass` sets this. |

## Provider Configuration

### OpenRouter (Primary)

```
OPENROUTER_API_KEY=sk-or-v1-your-key-here
DEFAULT_PROVIDER=openrouter
```

### Ollama (Local)

```bash
# Start Ollama + pull a model
docker compose -f src/config/docker/docker-compose.yml --profile local-llm up -d ollama
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
PYTHONPATH=src python src/scripts/cutover.py --agent sorter --provider ollama --model qwen3:7b
PYTHONPATH=src python src/scripts/cutover.py --validate --agent sorter
```

See [Local Models](local-models.md) for the full cutover guide.

## Environment variables

The committed template [`.env.example`](../.env.example) is the authoritative
knob list (copy to `.env` at the repo root). The full per-provider /
per-trace-sink configuration guide — covering THIS repo and
llm-entity-extraction together, including the Modal-vLLM flip and the shared
cross-repo contract — lives in the sibling repo:
[`docs/configuration.md`](https://github.com/Exios66/llm-entity-extraction/blob/main/docs/configuration.md).

Quick orientation:

| Group | Knobs |
|---|---|
| LLM provider | `DEFAULT_PROVIDER`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OLLAMA_BASE_URL`, `VLLM_BASE_URL`, `VLLM_API_KEY`, `GENERIC_API_KEY`, `GENERIC_BASE_URL` |
| Trace sinks | `OBSERVABILITY_PROVIDER`, `OBSERVABILITY_ENVIRONMENT`, `LANGFUSE_*`, `BRAINTRUST_*`, `PHOENIX_*` |
| Pipeline & API | `MAILROOM_*`, `WATCHER_*`, `OPS_MONITOR_INTERVAL_SECONDS`, `DATABASE_URL` |
| Logging | `LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` |
| LegalBench | `LEGALBENCH_EXPERIMENT_LOG`, `LEGALBENCH_SIBLING_REPO` |
