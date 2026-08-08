# Architecture

## Overview

Mailroom is a multi-agent legal document processing pipeline built on LangGraph. It ingests legal documents, classifies them, routes them to specialist agents for structured extraction, compiles matter records, and archives everything with a full audit trail.

## Architectural Diagram

```
Upload/Drop ──▶ /pipeline/inbox/ ──▶ [Watcher] ──▶ LangGraph run per document
                                                        │
                                    ┌───────────────────┼────────────────────┐
                                    ▼                    ▼                    ▼
                              Sorter (classify)   Confidence check     Boss (escalation)
                                    │                    │
                                    ▼                    ▼
                        /pipeline/classified/<type>/   /pipeline/review/ (human-in-the-loop)
                                    │
                                    ▼
                         Specialist Agent (extract per doc-type schema)
                                    │
                                    ▼
                              Reporter (compile matter record)
                                    │
                                    ▼
                              Catalog write (database)
                                    │
                                    ▼
                              Archivist (log + finalize)
                                    │
                                    ▼
                       /archive/<matter_id>/<doc_type>/

Parallel/independent:
  Boss (ops-monitor) ── sweeps the database + Langfuse periodically for stuck docs / error spikes
  Langfuse ── every LangGraph node emits a trace span (live deliberation viewer)
  Audit log ── every state transition writes a hash-chained entry, independent of Langfuse
```

## Core Components

### Watcher (`pipeline/watcher.py`)
- Uses `watchdog` to monitor `/pipeline/inbox/` for new files
- Debounces file events to avoid double-processing
- Claims files via atomic `os.rename` into `/pipeline/processing/<worker_id>/`
- Spawns a LangGraph run per document in a daemon thread

### LangGraph Engine (`graph/build_graph.py`)
- One graph execution per document
- 11 nodes forming a directed state machine
- SQLite-checkpointed for crash/resume
- Falls back to in-memory checkpointing when SQLite is unavailable

### LLM Client (`llm/client.py`, `llm/providers.py`)
- Thin OpenAI-compatible wrapper
- Provider-agnostic: OpenRouter, Ollama, vLLM, or any OpenAI-compatible endpoint
- Per-agent model selection from `config/taxonomy.yaml`
- Global provider override via `DEFAULT_PROVIDER` env var

### SQLite (`storage/db.py`, `storage/catalog.py`, `storage/audit_log.py`)
- SQLite (via SQLAlchemy 2.0 async + aiosqlite) by default — a single file, no server required
- Shared by the document/matter catalog and the audit log
- Three tables: `matters`, `documents`, `audit_log`
- `DATABASE_URL` env var can switch to Postgres

### Observability (`observability/`)
- Two interchangeable tracing backends: **Langfuse** (cloud or self-hosted, default) and **Braintrust**
- Selected via `OBSERVABILITY_PROVIDER` env (`auto` | `langfuse` | `braintrust` | `none`)
- Every LLM call is auto-traced: `llm/client.py:get_llm` wraps the OpenAI client (`langfuse.openai` patch or `braintrust.wrap_openai`), capturing prompt, response, tokens, latency
- Graceful noop fallback when no backend/keys are configured — pipeline runs unchanged

### Filesystem Bins (`pipeline/bins.py`)
- Human-legible pipeline state: `ls` any directory to see what's happening
- Atomic rename for claim safety (no external locking needed)
- Archive organized by `matter_id/doc_type/`

## Data Flow

### 1. Ingest
Document lands in `/pipeline/inbox/`. Watcher detects it, claims it atomically to `/pipeline/processing/<worker_id>/`. Manifest is created with `PipelineStage.PROCESSING`.

### 2. Classify (Sorter)
LLM call: reads document text, determines `doc_type` (contract, corporate_record, due_diligence, correspondence, compliance_filing) and confidence score.

### 3. Confidence Check
Conditional edge routing:
- **Confidence >= 0.85**: straight to extraction
- **Confidence < 0.70**: retry with alternate prompt
- **Still low after retry**: route to `/review/` (human)

### 4. Extract (Specialist)
Dynamic dispatch to the matching specialist agent based on `doc_type`. Each specialist:
- Has its own system prompt/personality
- Uses structured JSON output against a Pydantic schema
- Returns extraction data + confidence score

### 5. Extraction Confidence Check
Same three-way branch as classification, plus a fourth path:
- **Conflict with existing matter data**: route to Boss escalation
- **Low confidence**: retry → still low → human review
- **High confidence**: proceed to report compilation

### 6. Compile Report (Reporter)
LLM call: compiles all extracted data into a clean matter-record summary.

### 7. Catalog Write
Writes document and matter records to the database (best-effort — pipeline continues on failure).

### 8. Archive (Archivist)
- Moves file to `/archive/<matter_id>/<doc_type>/`
- Writes manifest sidecar JSON
- Writes hash-chained audit log entry
- Marks manifest `PipelineStage.ARCHIVED`

## State Machine Nodes

| Node | Agent | Purpose |
|---|---|---|
| `ingest` | — | Read file, create manifest, move to processing |
| `classify` | Sorter | Determine doc_type + confidence |
| `retry_classify` | Sorter | Re-classify with alternate prompt |
| `extract` | Specialist | Extract structured data per doc-type |
| `retry_extract` | Specialist | Re-extract with context from prior attempt |
| `human_review` | — | Pause for human decision |
| `boss_escalation` | Boss (in-graph) | Adjudicate conflicts |
| `compile_report` | Reporter | Synthesize matter-record entry |
| `catalog_write` | — | Write to database catalog |
| `archive` | Archivist | Move to archive, write audit log |

## Conditional Edges

```
classify ─┬─ confidence >= low ──▶ extract
          ├─ attempts <= retry_max ──▶ retry_classify
          └─ otherwise ──▶ human_review

extract ─┬─ confidence >= low + no conflict ──▶ compile_report
         ├─ conflict detected ──▶ boss_escalation
         ├─ attempts <= retry_max ──▶ retry_extract
         └─ otherwise ──▶ human_review

boss_escalation ─┬─ approved ──▶ compile_report
                 └─ review ──▶ human_review

human_review ─┬─ approved ──▶ compile_report
              └─ rejected ──▶ END (failed)
```

## Checkpointing

LangGraph checkpoints the full state after each node. On crash or restart:
- Any in-flight run resumes from the last completed node
- No document is lost and no document is processed twice
- SQLite-backed checkpointing (`data/checkpoints.db`) is the default; MemorySaver is the fallback if SQLite is unavailable

## Audit Trail

Every state transition writes an `AuditLogEntry` to the database. Each entry:
- Contains `prev_hash` (SHA-256 of the prior entry)
- Contains `entry_hash` (SHA-256 of `prev_hash` + entry content)
- Forms a tamper-evident chain — modifying any entry breaks all subsequent hashes
- Is independent of Langfuse (the audit log is the compliance record)
- Can be verified via the `/audit/{doc_id}` API endpoint or `schemas/audit.py:verify_chain()`

## Boss Agent — Dual Role

The Boss agent has two separate invocation paths sharing one persona:

1. **In-graph (`boss_escalation` node)**: synchronously adjudicates conflicts within a single document's run.
2. **Ops-monitor (`pipeline/ops_monitor.py`)**: separate scheduled process (default every 5 minutes) that queries the catalog for systemic issues: stuck documents, error-rate spikes, review backlogs.
