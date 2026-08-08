# Configuration

All configuration lives in `config/taxonomy.yaml` — **nothing is hardcoded**. Adding a document class or adjusting thresholds never requires touching agent code.

## taxonomy.yaml Structure

```yaml
pipeline:
  bins:              # Filesystem paths
  confidence:        # Routing thresholds
  doc_classes:       # Document type definitions
  file_extensions:   # Accepted file types
  agents:            # Per-agent model/provider configs
```

## Pipeline Bins

Defines where files live during and after processing. `{base_dir}` resolves to `MAILROOM_BASE_DIR` env var (default: `./data`).

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

## Confidence Thresholds

These control the branching logic in `graph/routing.py`:

| Key | Default | Behavior |
|---|---|---|
| `high` | 0.85 | Above this: proceed without retry |
| `low` | 0.70 | Below this: trigger retry |
| `retry_max` | 1 | Max retries before human review |
| `conflict_threshold` | 0.3 | Confidence gap below this: potential conflict |

```yaml
confidence:
  high: 0.85
  low: 0.70
  retry_max: 1
  conflict_threshold: 0.3
```

**Route decision matrix:**

| Classification Confidence | Attempts | Route |
|---|---|---|
| >= 0.70 | Any | Extract |
| < 0.70 | 1 | Retry classify |
| < 0.70 | 2+ | Human review |
| Unknown doc type | Any | Human review |

## Document Classes

Each entry defines a document type the pipeline can handle. To add a new type:

1. Add entry here in `doc_classes`
2. Create extraction schema in `schemas/documents.py`
3. Create specialist agent in `agents/`
4. Register in `EXTRACTION_SCHEMAS` dict and `graph/build_graph.py` dispatch

```yaml
doc_classes:
  - key: contract
    label: "Contract / Agreement"
    schema: ContractExtraction
    specialist: contracts_specialist
    description: "Formal agreements: M&A, vendor, employment, NDAs, etc."

  - key: corporate_record
    label: "Corporate Record"
    schema: CorporateRecordExtraction
    specialist: corporate_records_specialist
    description: "Bylaws, resolutions, board minutes, cap table entries"

  - key: due_diligence
    label: "Due Diligence"
    schema: DueDiligenceExtraction
    specialist: due_diligence_specialist
    description: "Checklists, disclosure schedules, diligence memos"

  - key: correspondence
    label: "Correspondence"
    schema: CorrespondenceExtraction
    specialist: correspondence_specialist
    description: "Letters, emails, memos, notices"

  - key: compliance_filing
    label: "Compliance Filing"
    schema: ComplianceFilingExtraction
    specialist: compliance_specialist
    description: "SEC filings, state registrations, regulatory submissions"
```

## File Extensions

```yaml
file_extensions:
  - .txt
  - .pdf
  - .docx
  - .md
```

## Agent Model Mapping

Per-agent provider and model configuration. This is where cutover happens:

```yaml
agents:
  sorter:
    provider: openrouter      # ollama, vllm, generic
    model: openai/gpt-4o      # qwen3:7b, llama3.1:8b, etc.
    temperature: 0.1

  contracts_specialist:
    provider: openrouter
    model: openai/gpt-4o
    temperature: 0.1
  # ... (one per agent)
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API key |
| `DEFAULT_PROVIDER` | No | `openrouter` | Global provider override |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///<MAILROOM_BASE_DIR>/mailroom.db` | Async database URL. SQLite by default; set a Postgres URL to switch |
| `OBSERVABILITY_PROVIDER` | No | `auto` | Tracing backend: `auto` \| `langfuse` \| `braintrust` \| `none` |
| `LANGFUSE_PUBLIC_KEY` | No | `pk-lf-local` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | No | — | Langfuse secret key (present ⇒ `auto` picks Langfuse) |
| `LANGFUSE_HOST` | No | `http://localhost:3000` | Langfuse server (`LANGFUSE_BASE_URL` accepted as alias) |
| `BRAINTRUST_API_KEY` | No | — | Braintrust API key (present ⇒ `auto` picks Braintrust) |
| `BRAINTRUST_PROJECT` | No | `mailroom` | Braintrust project name |
| `MAILROOM_BASE_DIR` | No | `./data` | Pipeline filesystem root (also where SQLite files live) |
| `OLLAMA_BASE_URL` | No | `http://localhost:11434/v1` | Ollama server |
| `VLLM_BASE_URL` | No | `http://localhost:8000/v1` | vLLM server |
| `GENERIC_API_KEY` | No | — | Generic provider key |
| `GENERIC_BASE_URL` | No | — | Generic provider URL |

## Provider Cutover

### Global (all agents):

```bash
export DEFAULT_PROVIDER=ollama
```

### Per-agent (recommended):

```yaml
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

### Hybrid (mixed providers):

```yaml
agents:
  sorter:
    provider: ollama
    model: qwen3:7b
  contracts_specialist:
    provider: openrouter
    model: openai/gpt-4o
```

See [Local Model Cutover](Local-Model-Cutover) for the full guide.
