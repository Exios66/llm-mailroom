# FAQ

## What is Mailroom?

Mailroom is a multi-agent pipeline that ingests legal documents, classifies them, routes them to specialist agents for structured extraction, compiles the results into a matter record, and archives everything with a full audit trail.

## What document types does it handle?

Seven first-class document classes:

- **Contracts** (MSAs, NDAs, employment agreements, etc. — 25 CUAD subtypes)
- **Corporate Records** (bylaws, resolutions, board minutes, cap tables)
- **Due Diligence** (checklists, disclosure schedules, diligence memos)
- **Correspondence** (demand letters, legal notices, memos — corpus from the CMU Enron dataset)
- **Compliance Filings** (SEC filings, state registrations, regulatory docs)
- **Court Opinions** (published decisions, memorandum opinions, rulings)
- **Insurance Claims** (FNOL forms, adjuster reports, demand packages, coverage determinations, denial letters)

Adding a new class touches a known surface list (schema registry, taxonomy, specialist agent, graph dispatch, classifier vocabulary, sorter prompt) — see [the repo docs](https://github.com/Exios66/llm-mailroom/tree/main/docs).

## Why LangGraph instead of plain agent chains?

LangGraph provides:
- A defined, explicit state machine — agents don't freely negotiate what happens next
- Conditional edges for confidence-based routing, plus exception lanes: an agent second-opinion reviewer for exhausted medium-band classifications, and a gated judge→arbiter completeness-verification path for grounded extractions
- Human-in-the-loop via review routing (`human_review` node) for ambiguous or failed cases
- In-memory checkpointing by default; opt-in SQLite persistence (`MAILROOM_CHECKPOINTER=sqlite`) for resume-across-restart experiments

## Can I use local models instead of OpenRouter?

Yes. Set `DEFAULT_PROVIDER=ollama` in `.env`, or configure per-agent in `config/taxonomy.yaml`. See [Local Model Cutover](https://github.com/Exios66/llm-mailroom/blob/main/docs/local-models.md).

## What happens if the database is unavailable?

The pipeline degrades gracefully:
- LangGraph checkpointer falls back to MemorySaver
- Catalog writes are best-effort (pipeline continues without them)
- Audit log entries are best-effort
- The manifest JSON sidecar (archived with each file) is always written — filesystem-based durability

## What happens if Langfuse is unavailable?

The system runs without it. The `observability/langfuse_setup.py` module has a noop client that handles all calls gracefully. The audit log is independent of Langfuse.

## How does document claiming work?

Watcher uses `os.rename()` to atomically move files from `/pipeline/inbox/` to `/pipeline/processing/<worker_id>/`. `os.rename` is atomic on the same filesystem, so no two workers can claim the same file. No external locking needed.

## How does the audit trail work?

Every state transition writes an `AuditLogEntry` to the `audit_log` table. Each entry is SHA-256 hashed with its predecessor's hash, forming a tamper-evident chain. The chain can be verified via the `/audit/{doc_id}` endpoint. Audit entries are also written by the Boss on escalation.

## What's the Boss agent?

The Boss has two roles:
1. **In-graph**: adjudicates conflicts when extraction data contradicts existing matter records
2. **Ops-monitor**: separate process that sweeps the catalog for stuck documents, error spikes, and review backlogs

Both share the same system-prompt "voice" but are triggered differently and see different data.

## How do I add a new matter?

Matters are auto-created when you upload a document with a new `matter_id`. You don't need to create matters explicitly. The catalog records the matter on first document ingestion.

## Does this support PDFs and DOCX?

The `file_extensions` in `config/taxonomy.yaml` include `.pdf` and `.docx`. PDFs are transcribed by `agents/pdf_transcriber.py` and images by `agents/image_extractor.py`. DOCX is read as raw text via `read_text` in `ingest_node` (for production-grade DOCX support you'd add a `python-docx` extraction step).

## What's the scale target?

v1 targets pilot scale: dozens of documents/day per matter. The threaded watcher and single-process design is sufficient. For higher volumes, Redis-based queuing and multiple workers are planned as deferred work.

## Where are my files stored?

During processing: `data/pipeline/` (inbox, processing, classified, review, failed)
After processing: `data/archive/<matter_id>/<doc_type>/`
Manifests: `data/manifests/<doc_id>.json`

`MAILROOM_BASE_DIR` controls the root (`./data` by default).

## How do I monitor the pipeline?

Three ways:
1. **Langfuse UI** (`http://localhost:3000`) — live traces of every LLM call
2. **`/ops/status` endpoint** — pipeline-wide metrics (stuck docs, review backlog, error rates)
3. **Ops monitor** — automated Boss sweeps with alerts

## Is this production-ready?

For pilot scale (dozens of documents/day) with human oversight: yes. For enterprise production with multi-tenant isolation, RBAC, and high-availability: this is the foundation but needs the deferred work in the roadmap (Redis queues, richer web UI, full RBAC, etc.).
