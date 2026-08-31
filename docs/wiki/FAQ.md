# FAQ

## What is Mailroom?

Mailroom is a multi-agent pipeline that ingests legal documents, classifies them, routes them to specialist agents for structured extraction, compiles the results into a matter record, and archives everything with a full, tamper-evident audit trail. One **LangGraph state machine run per document** (13 nodes), with retries, second-opinion review lanes, judge/arbiter quality gates, and human review routing.

## What document types does it handle?

Six live taxonomy classes:

- **Contracts** (CUAD commercial agreements — 25 agreement families: MSAs, NDAs, licenses, distribution, …)
- **Merger Agreements** (MAUD — a distinct class reusing the contracts specialist)
- **Corporate Records** (articles, bylaws, powers of attorney, rights instruments, other)
- **Correspondence** (demand letters, legal notices, memos — corpus from the CMU Enron dataset)
- **Compliance Filings** (SEC filings, state filings)
- **Insurance Claims** (FNOL lines + CMS DE-SynPUF `carrier`/`inpatient`/`outpatient`/`pde` claim types; coverage determinations approved/denied/partial)

**Due diligence** and **court opinions** were retired from the live pipeline in v0.5.0 — they classify as `unknown` (human review), and there is no specialist dispatch for them. LegalBench remains the court-opinion benchmark surface.

Adding a new class touches a known surface list (schema registry, taxonomy, specialist agent, graph dispatch, classifier vocabulary, sorter prompt) — see [the repo docs](https://github.com/Exios66/llm-mailroom/tree/main/docs).

## Why LangGraph instead of plain agent chains?

LangGraph provides:
- A defined, explicit state machine — agents don't freely negotiate what happens next
- Conditional edges for confidence-based routing, plus exception lanes: an agent second-opinion reviewer for exhausted medium-band classifications (Lane A), and a gated judge→arbiter completeness-verification path for grounded extractions (Lane B)
- Human-in-the-loop via review routing (`human_review` node) for ambiguous or failed cases
- In-memory checkpointing by default; opt-in SQLite persistence (`MAILROOM_CHECKPOINTER=sqlite`) for resume-across-restart experiments

## Can I use local models instead of OpenRouter?

Yes. Set `DEFAULT_PROVIDER=ollama` in `.env`, or configure per-agent in `config/taxonomy.yaml`. A pre-built Modal+vLLM serving app lives in `deploy/` (see [`docs/local-models.md`](https://github.com/Exios66/llm-mailroom/blob/main/docs/local-models.md)).

## What happens if the database is unavailable?

The pipeline degrades gracefully:
- LangGraph checkpointer falls back to MemorySaver
- Catalog writes are best-effort (pipeline continues without them)
- Audit log entries are best-effort
- The manifest JSON sidecar (archived with each file) is always written — filesystem-based durability

## What happens if Langfuse is unavailable?

The system runs without it. Tracing is backend-agnostic via `observability/tracing.py`: `OBSERVABILITY_PROVIDER=auto` picks Langfuse if a key is set, else Braintrust if a key is set, else the local cost-free Arize Phoenix backend, else `none` — tracing never silently turns off, and the audit log is independent of any observability backend.

## How does document claiming work?

Files move only through `pipeline/bins.py` helpers (`claim_file`, `move_to_*`, `save_manifest`) — never direct `os.rename`/`shutil.move` in node/agent code. The watcher atomically claims files from `/pipeline/inbox/` into `/pipeline/processing/<worker_id>/`; `os.rename` (via `move_to_*`) is atomic on the same filesystem, so no two workers can claim the same file.

## How does the audit trail work?

Every state transition writes an `AuditLogEntry` to the `audit_log` table. Each entry is SHA-256 hashed with its predecessor's hash, forming a tamper-evident chain. The chain can be verified via the `/audit/{doc_id}` endpoint. Audit entries are also written by the Boss on escalation.

## What's the Boss agent?

The Boss has two roles:
1. **In-graph** (`boss_escalation` node): adjudicates conflicts when extraction data contradicts existing matter records or confidence stays low after retries
2. **Ops-monitor** (`pipeline/ops_monitor.py`): separate scheduled process that sweeps the catalog for stuck documents, error spikes, and review backlogs

Both share the same system-prompt "voice" but are triggered differently and see different data.

## How do I add a new matter?

Matters are auto-created when you upload a document with a new `matter_id`. You don't need to create matters explicitly. The catalog records the matter on first document ingestion.

## Does this support PDFs and images?

The `file_extensions` in `config/taxonomy.yaml` include `.txt`, `.pdf`, `.docx`, `.md`, plus image types (jpg/png/gif/webp/tiff/bmp). PDFs are transcribed by `agents/pdf_transcriber.py` (`pdfplumber`/`pypdf` direct pass for text-based files, LLM markdown reformat for scanned/garbled) and images by `agents/image_extractor.py`. Vision ingestion (page images appended to prompts for vision-capable models) is additive — the full text transcription is always the message body.

## What's the scale target?

Pilot scale: dozens of documents/day per matter. The threaded watcher and single-process design is sufficient. For higher volumes, Redis-based queuing and multiple workers are planned as deferred work.

## Where are my files stored?

Configurable bins under `MAILROOM_BASE_DIR` (default `./data`):
- During processing: `pipeline/inbox`, `pipeline/processing/<worker_id>/` (plus `pipeline/classified/`, `pipeline/review/`, `pipeline/failed/`)
- After processing: `archive/<matter_id>/<doc_type>/` with a manifest JSON sidecar in `manifests/<doc_id>.json`
- Catalog + audit log: SQLite `mailroom.db` (Postgres via `DATABASE_URL`); crash-resume checkpoints: `checkpoints.db`

## How do I monitor the pipeline?

Four ways:
1. **The-Mailroom Observatory** — live floor [`Lucius-Morningstar/mailroom-observatory`](https://huggingface.co/spaces/Lucius-Morningstar/mailroom-observatory) plus this producer Space (`mailroom-producer`). Langfuse for envelopes; Inbox / REVIEW go through `MAILROOM_PIPELINE_URL` ([PR #30](https://github.com/Exios66/The-Mailroom/pull/30)). Pairing: [deploy/space/PAIRING.md](https://github.com/Exios66/llm-mailroom/blob/main/deploy/space/PAIRING.md).
2. **Langfuse UI** (`https://us.cloud.langfuse.com` or `http://localhost:3000`) — live traces of every LLM call
3. **`/ops/status` endpoint** — pipeline-level metrics (stuck docs, review backlog, error rates, first-pass rate)
4. **Ops monitor** — automated Boss sweeps with alerts

## Why does Observatory REVIEW / Queue a document return 503?

The visualizer is configured without a reachable producer. Set
`MAILROOM_PIPELINE_URL`, `MAILROOM_PIPELINE_TOKEN` (same as this API's
`MAILROOM_API_TOKEN`), and `MAILROOM_PIPELINE_API_PREFIX=/v1` on The-Mailroom.
A Hugging Face Space Observatory cannot use `http://127.0.0.1:8000` — that
loopback is inside the Observatory container, not this API. Publish this
repo's producer Space and point the URL at `https://lucius-morningstar-mailroom-producer.hf.space`.

## Is this production-ready?

For pilot scale (dozens of documents/day) with human oversight: yes. For enterprise production with multi-tenant isolation, RBAC, and high-availability: this is the foundation but needs the deferred work in the roadmap (Redis queues, richer web UI, full RBAC, etc.).