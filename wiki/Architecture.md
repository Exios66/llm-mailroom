# Architecture

## System Diagram

```
Upload/Drop --> /pipeline/inbox/ --> [Watcher] --> LangGraph run per document
                                                        |
                                    +-------------------+-------------------+
                                    |                   |                   |
                               Sorter (classify)  Confidence check   Boss (escalation)
                                    |                   |
                                    |                   v
                                    |         /pipeline/review/ (human-in-the-loop)
                                    v
                        /pipeline/classified/<type>/
                                    |
                                    v
                         Specialist Agent (extract per doc-type schema)
                                    |
                                    v
                              Reporter (compile matter record)
                                    |
                                    v
                              Catalog write (Postgres)
                                    |
                                    v
                              Archivist (log + finalize)
                                    |
                                    v
                       /archive/<matter_id>/<doc_type>/

Parallel/independent:
  Boss (ops-monitor) — sweeps Postgres + Langfuse periodically
  Langfuse — every LangGraph node emits a trace span
  Audit log — every state transition writes a hash-chained entry
```

## Core Components

### Watcher (`pipeline/watcher.py`)
- **watchdog**-based filesystem monitor on `/pipeline/inbox/`
- Debounces file events to avoid double-processing
- Claims files via atomic `os.rename` into `/pipeline/processing/<worker_id>/`
- Spawns a LangGraph run per document in a daemon thread

### LangGraph Engine (`graph/build_graph.py`)
- **11 nodes** forming a directed state machine
- One graph execution per document — each document is independent
- **Postgres-checkpointed** for crash/resume (falls back to MemorySaver)
- All routing logic in `graph/routing.py` — conditional edges driven by confidence thresholds from `config/taxonomy.yaml`

### LLM Client (`llm/client.py`, `llm/providers.py`)
- Thin **OpenAI-compatible** wrapper
- Provider-agnostic: OpenRouter, Ollama, vLLM, or any OpenAI-compatible endpoint
- Per-agent model selection from `config/taxonomy.yaml`
- Global override via `DEFAULT_PROVIDER` env var

### Postgres
- Shared by: LangGraph **checkpointer**, document/matter **catalog**, and **audit log**
- SQLAlchemy 2.0 async with psycopg
- Three tables: `matters`, `documents`, `audit_log`

### Langfuse
- Self-hosted in Docker Compose (on-prem — sensitive data stays internal)
- Every LangGraph node wrapped with trace spans
- Graceful noop fallback when unavailable — pipeline runs without it

### Filesystem Bins
- Human-legible pipeline state — `ls` any directory to see status
- Atomic rename for claim safety — no external locking needed
- Archive organized by `matter_id/doc_type/` for easy browsing

## State Machine

### Nodes

| # | Node | Agent | Purpose |
|---|---|---|---|
| 1 | `ingest` | — | Read file, create manifest, move to processing |
| 2 | `classify` | Sorter | Determine doc_type + confidence |
| 3 | `retry_classify` | Sorter | Re-classify with alternate prompt |
| 4 | `extract` | Specialist | Extract structured data per doc-type |
| 5 | `retry_extract` | Specialist | Re-extract with prior attempt context |
| 6 | `human_review` | — | Pause for human decision (LangGraph interrupt) |
| 7 | `boss_escalation` | Boss | Adjudicate data conflicts |
| 8 | `compile_report` | Reporter | Synthesize matter-record entry |
| 9 | `catalog_write` | — | Write to Postgres catalog |
| 10 | `archive` | Archivist | Move to archive, write audit entry |

### Conditional Edges

```
classify --+-- confidence >= low ------------ extract
           +-- attempts <= retry_max -------- retry_classify
           +-- otherwise ------------------- human_review

extract ---+-- confidence >= low, no conflict -- compile_report
           +-- conflict detected -------------- boss_escalation
           +-- attempts <= retry_max ---------- retry_extract
           +-- otherwise ---------------------- human_review

boss_escalation --+-- approved -- compile_report
                  +-- review --- human_review

human_review --+-- approved -- compile_report
               +-- rejected -- END (failed)
```

## Data Flow

1. **Ingest**: File lands in inbox → claimed atomically to processing → manifest created
2. **Classify**: LLM reads text → determines doc_type + confidence
3. **Confidence Check**: High → extract; Low → retry; Still low → human review
4. **Extract**: Dynamic dispatch to matching specialist → structured JSON output
5. **Extraction Check**: Same three-way + conflict detection → Boss escalation
6. **Compile Report**: LLM synthesizes all extracted data into matter-record summary
7. **Catalog Write**: Postgres `documents` and `matters` tables (best-effort)
8. **Archive**: Move file to `/archive/<matter_id>/<doc_type>/` + manifest sidecar + audit entry

## Filesystem Layout

```
data/
  pipeline/
    inbox/              # New uploads
    processing/<id>/    # Claimed by worker
    classified/<type>/  # Sorted, awaiting specialist
    review/             # Human review required
    failed/             # Unrecoverable errors
  archive/
    <matter_id>/<type>/ # Final durable home
  manifests/
    <doc_id>.json       # Self-contained DocumentManifest
```

## Audit Trail Design

Every state transition writes an `AuditLogEntry` to Postgres:

```
entry_1 (prev_hash: "")         -- classified by sorter
    |
    v
entry_2 (prev_hash: hash_1)     -- extracted by contracts_specialist
    |
    v
entry_3 (prev_hash: hash_2)     -- archived by archivist
```

- Each entry is SHA-256 hashed with its predecessor's hash
- Tampering with any entry breaks all subsequent hashes
- The chain can be verified via `GET /audit/{doc_id}` or `verify_chain()`
- Independent of Langfuse — this is the actual compliance record

## Boss Agent — Dual Role

The Boss has **two separate invocation paths** sharing one persona:

1. **In-graph** (`boss_escalation` node): synchronously adjudicates conflicts within a document's pipeline run
2. **Ops-monitor** (`pipeline/ops_monitor.py`): separate scheduled process sweeping the catalog for systemic issues

Both share the same system prompt voice — consistent persona, different scopes of data.
