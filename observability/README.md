# `observability/` — Tracing every LLM call

## What this folder is (plain English)

This logs every AI call the pipeline makes — classification, extraction, reports, the Boss — so you can see exactly what prompt went out, what the model answered, how long it took, and how many tokens it cost. Two backends are supported; you pick one with a single env var.

- **Langfuse** — the default. Works with their cloud (`us.cloud.langfuse.com`) or a self-hosted instance. Dashboard at the Langfuse UI.
- **Braintrust** — an alternative. Add your `BRAINTRUST_API_KEY`, flip one env var, and the same calls appear in Braintrust instead.

**It's completely optional and safe.** If no backend is configured (or the keys are missing/wrong), everything no-ops and the pipeline runs exactly as if tracing were off.

## Configuration

```bash
# .env
OBSERVABILITY_PROVIDER=auto     # auto | langfuse | braintrust | none

# Langfuse (cloud example)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://us.cloud.langfuse.com   # LANGFUSE_BASE_URL is an alias

# Braintrust (alternative)
BRAINTRUST_API_KEY=
BRAINTRUST_PROJECT=mailroom
```

`auto` picks Langfuse if `LANGFUSE_SECRET_KEY` is set, else Braintrust if `BRAINTRUST_API_KEY` is set, else nothing.

## Technical reference

- **`tracing.py`** — the facade the rest of the app uses.
  - `resolve_provider_name()` → `langfuse` | `braintrust` | `none` (reads `OBSERVABILITY_PROVIDER`, default `auto`).
  - `instrument_openai_client(client)` — wrap the OpenAI client with the active backend, or return it unchanged. This is the only integration point; it's called from `llm/client.py:get_llm` (`instrument_client`).
  - `flush()` — push queued events to the backend.
  - `register_atexit_flush()` — optional; flushes on process exit.
- **`langfuse_setup.py`** — Langfuse backend (langfuse ≥ 4.x).
  - `get_langfuse_client()` — lazily builds `Langfuse(public_key, secret_key, host)` from env; returns `_NoopLangfuse` when there's no `LANGFUSE_SECRET_KEY` or init fails. `_resolve_host()` prefers `LANGFUSE_HOST`, falls back to `LANGFUSE_BASE_URL`.
  - `instrument_openai_client(client)` — inits the client then imports `langfuse.openai`. langfuse 4.x instruments by monkeypatching `openai.resources.chat.completions.Completions.create` at import time, so the **original client is returned unchanged** and every OpenAI call in the process is traced. (The old `client.trace()` API was removed in langfuse 4.x — this module was rewritten for it.)
- **`braintrust_setup.py`** — Braintrust backend.
  - `configure()` — `braintrust.init(project=..., api_key=...)`; idempotent; no-op without `BRAINTRUST_API_KEY`.
  - `instrument_openai_client(client)` — `braintrust.wrap_openai(client)` (keeps the same interface).
  - `flush_braintrust()` — `braintrust.flush()`.
- **How it connects to the pipeline:**
  - **Every LLM call** gets its client from `get_llm(agent_name)`, which passes it through `instrument_client` → `tracing.instrument_openai_client`. So sorter, specialists, reporter, boss, and image/PDF extraction are all traced with zero changes to agent code.
  - **Structured, nested traces per document** (Langfuse best practices — see the installed `langfuse` skill under `.opencode/skills/langfuse/`):
    - One trace per document run, named `document-pipeline`, with a **deterministic trace id** seeded from the file name (correlates the trace with the document in our own DB). Reprocessing the same file reuses the same trace id, so all attempts on one document appear in a single trace.
    - `session_id = matter_id` — every document of a matter groups into one Langfuse session.
    - Root span input = `{filename, matter_id}`; output = `{stage, doc_type, confidence}`.
    - Each graph node is wrapped in a stable, verb-first span (`ingest-document`, `classify-document`, `extract-fields`, `compile-report`, `write-catalog`, `archive-document`, ...) with curated input (identifiers, never raw document text) and output (stage/confidence). LLM generations nest under the node span that issued them.
    - `tags=["mailroom"]`, `environment` from `OBSERVABILITY_ENVIRONMENT` (optional).
    - Traces are flushed after every document run so they appear promptly.
- **Security note:** traces contain full prompts/responses (the legal document content) — deliberate, for auditability. Access-control the Langfuse UI (root README → Security).
- Tests: `tests/test_observability.py` (provider resolution, env aliasing, noop-safe trace helpers). Tests never hit the network.
