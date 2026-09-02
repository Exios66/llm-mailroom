# `graph/` — The state machine that runs each document

## What this folder is (plain English)

This is the **engine room**. Mailroom is built on **LangGraph**: for every document, it runs a fixed "assembly line" of steps (called *nodes*). Each node takes the document's current state, does one thing, and hands the result to the next node. The file `build_graph.py` wires all the nodes together.

The journey of a document:

```
ingest → classify → (extract | retry_classify | human_review) → extract → compile_report
      → catalog_write → archive
```

At a few points a *conditional edge* (in `routing.py`) looks at the LLM's **confidence score** and decides what happens next: proceed, retry with a stronger prompt, send to the Boss, or route to a human.

## The 11 nodes (in `build_graph.py`)

| Node | What it does |
|---|---|
| `ingest` | Reads the file, deterministic `normalize-intake`, creates the manifest, moves file to `processing/` |
| `classify` | Sorter LLM decides `doc_type` + confidence |
| `retry_classify` | Re-classify with a "re-evaluate" prompt when confidence was low |
| `extract` | Routes to the right specialist LLM, stores `extracted_data` |
| `retry_extract` | Re-extract with the previous attempt included as context |
| `human_review` | Parks the file in `review/` (idempotent upsert) and pauses the graph with LangGraph `interrupt()` until a human `Command(resume=...)` |
| `boss_escalation` | Boss LLM adjudicates conflicts / repeated failures |
| `compile_report` | Reporter LLM writes the matter-record summary |
| `catalog_write` | Writes the document + matter to the database (best-effort) |
| `archive` | Moves file to `archive/<matter_id>/<type>/`, writes audit entry |

## Technical reference

- **Node contract:** `def node(state: DocumentState) -> dict[str, Any]` — returns only the fields it changed; LangGraph merges them into state.
- `state.py` — `DocumentState`, a `TypedDict` with all pipeline fields (`doc_type`, `classification_confidence`, `extracted_data`, `stage`, …).
- `routing.py` — pure functions returning the name of the next node: `after_classify`, `after_retry_classify`, `after_extraction`, `after_retry_extraction`, `after_boss`, `after_human_review`, `after_report`, plus Lane A/B routers (`after_review_classify`, `after_judge`, `after_arbiter`). Ground-truth class misses go to Lane A even at 0.99 confidence; hollow / low-coverage extracts retry then review; a failed `compile_report` withholds `catalog_write`. Transient provider errors self-loop on the SAME node (per-node `transient_retries_<node>` budget); they never bounce retry nodes back to first-pass `classify`/`extract`. Thresholds come from `config/taxonomy.yaml` → `get_confidence_thresholds()`, never hardcoded.
- `build_graph.py` also handles:
  - Text extraction for images/PDFs (`_read_file_text` → `agents/image_extractor.py`, `agents/pdf_transcriber.py`).
  - Deterministic intake normalize (`agents/intake.py` → `llm_dojo_scoring.intake`) after transcription; nested span `normalize-intake` (The-Mailroom maps it to INGEST). Scored via `get_suite("intake")`.
  - Specialist dispatch via `_build_specialist_dispatch()` — **config-driven**: it walks `doc_classes` in `config/taxonomy.yaml` and maps each `specialist:` name to its extraction function (6 configured classes — the canonical five plus the `status: retired` `compliance_filing` remnant — / 5 specialists: contracts also covers MAUD `merger_agreement`, plus corporate records, correspondence, compliance, insurance claims). Graph construction asserts dispatch keys equal taxonomy keys; a missing arm fails fast instead of silently stub-extracting. `unknown` is a sorter routing token, not a dispatch key. Adding an agent means adding a taxonomy entry + dispatch case.
  - The **checkpointer** (`_build_checkpointer`): **MemorySaver by default**, held on a process-level compiled graph (`get_compiled_graph`) so `interrupt()` HITL can resume in-process. Filesystem `review/` is the durable park; `resume_from_review` uses `Command(resume=...)` when a checkpoint exists, else re-invokes from extract. `MAILROOM_CHECKPOINTER=sqlite` opts into on-disk `SqliteSaver` at `data/checkpoints.db`.
  - **Chunked extraction** (`_run_chunked_extraction`): every live specialist (contracts, corporate records, correspondence, compliance, insurance). Window size is capped at the agent's `max_input_chars`.
  - `run_pipeline(file_path, matter_id)` — convenience entrypoint that reuses the process-level graph and runs one document.
  - **Judge gating**: for grounded runs with deterministic field scoring, `_emit_pipeline_result` suppresses the `pipeline-result` generation when the verdict is unambiguous (see `field_scoring.type_bands` in `taxonomy.yaml`) — saving both LLM-as-judge evaluator calls.
- Conditionals are wired with `add_conditional_edges("classify", after_classify, {...})`; `after_classify` can return `"retry_classify"`, `"extract"`, or `"human_review"`.
- Architecture doc: `docs/architecture.md`.
