# `graph/` — The state machine that runs each document

## What this folder is (plain English)

This is the **engine room**. Mailroom is built on **LangGraph**: for every document, it runs a fixed "assembly line" of steps (called *nodes*). Each node takes the document's current state, does one thing, and hands the result to the next node. The file `build_graph.py` registers the nodes; `route_spec.py` declares which node follows which, and `build_graph()` wires those edges through `wire_edges`.

The journey of a document:

```
intake → classify → (extract | retry_classify | human_review) → extract → compile_report
      → catalog_write → archive
```

At a few points a *conditional edge* (in `routing.py`) looks at the LLM's **confidence score** and decides what happens next: proceed, retry with a stronger prompt, send to the Boss, or route to a human.

## The 14 nodes (in `build_graph.py`)

| Node | What it does |
|---|---|
| `intake` | Reads the file, deterministic `normalize-intake`, gated LLM triage/clean/prepare, creates the manifest, moves file to `processing/` |
| `classify` | Sorter LLM decides `doc_type` + confidence |
| `retry_classify` | Re-classify with a "re-evaluate" prompt when confidence was low |
| `review_classify` | Lane A: an independent reviewer agent classifies a medium-band document without seeing the sorter's answer; agreement or a high-confidence override can proceed; a decision-model result that is not `extract` (or a failed review) goes to `classification_arbiter`, and anything else keeps the sorter label for human review |
| `classification_arbiter` | Arbitrates a parked or failed decision review before a person sees it: picks the sorter's class, the reviewer's class or human review, and continues only above the calibrated arbiter cutoff; a document with no readable text skips the arbiter and goes straight to `human_review` |
| `extract` | Routes to the right specialist LLM, stores `extracted_data` |
| `retry_extract` | Re-extract with the previous attempt included as context |
| `judge_verify` | Lane B: gated completeness judge on the specialist's extraction (`complete` proceeds; `partial` / `incomplete` go to `arbiter`) |
| `arbiter` | Lane B: bounded decision on a failed judge verdict (`accept_with_caveats`, retry with a fix list, or `human_review`) |
| `human_review` | Parks the file in `review/` (idempotent upsert) and pauses the graph with LangGraph `interrupt()` until a human `Command(resume=...)` |
| `boss_escalation` | Boss LLM adjudicates conflicts / repeated failures |
| `compile_report` | Procedural reporter assembles the matter-record summary (no LLM) |
| `catalog_write` | Writes the document + matter to the database (best-effort) |
| `archive` | Moves file to `archive/<matter_id>/<type>/`, writes audit entry |

## Technical reference

- **Node contract:** `def node(state: DocumentState) -> dict[str, Any]` — returns only the fields it changed; LangGraph merges them into state.
- `state.py` — `DocumentState`, a `TypedDict` with all pipeline fields (`doc_type`, `classification_confidence`, `extracted_data`, `stage`, …).
- `routing.py` — pure functions returning the name of the next node: `after_classify`, `after_retry_classify`, `after_extraction`, `after_retry_extraction`, `after_boss`, `after_human_review`, `after_report`, plus Lane A/B routers (`after_review_classify`, `after_judge`, `after_arbiter`). Ground-truth class misses go to Lane A even at 0.99 confidence; hollow / low-coverage extracts retry then review; a failed `compile_report` withholds `catalog_write`. Transient provider errors self-loop on the SAME node (per-node `transient_retries_<node>` budget); they never bounce retry nodes back to first-pass `classify`/`extract`. Thresholds come from `config/taxonomy.yaml` → `get_confidence_thresholds()`, never hardcoded.
- `build_graph.py` also handles:
  - Text extraction for images/PDFs (`_read_file_text` → `agents/image_extractor.py`, `agents/pdf_transcriber.py`).
  - Deterministic intake normalize (`agents/intake.py` → `llm_dojo_scoring.intake`) after transcription; nested span `normalize-intake` (The-Mailroom maps it to INGEST). Scored via `get_suite("intake")`.
  - Specialist dispatch via `_build_specialist_dispatch()` — **config-driven**: it walks `doc_classes` in `config/taxonomy.yaml` and maps each `specialist:` name to its extraction function (5 configured classes, 5 specialists: `contract` → `contracts_specialist`, `merger_agreement` (MAUD) → `merger_agreement_specialist`, `corporate_record` → `corporate_records_specialist`, `correspondence` → `correspondence_specialist`, `insurance_claim` → `insurance_claims_specialist`). Graph construction asserts dispatch keys equal taxonomy keys; a missing arm fails fast instead of silently stub-extracting. `unknown` is a sorter routing token, not a dispatch key. Adding an agent means adding a taxonomy entry + dispatch case.
  - The **checkpointer** (`_build_checkpointer`): **MemorySaver by default**, held on a process-level compiled graph (`get_compiled_graph`) so `interrupt()` HITL can resume in-process. Filesystem `review/` is the durable park; `resume_from_review` uses `Command(resume=...)` when a checkpoint exists, else re-invokes from extract. `MAILROOM_CHECKPOINTER=sqlite` opts into on-disk `SqliteSaver` at `data/checkpoints.db`.
  - **Chunked extraction** (`_run_chunked_extraction`): every live specialist (contracts, merger agreements, corporate records, correspondence, insurance). Window size is capped at the agent's `max_input_chars`.
  - `run_pipeline(file_path, matter_id)` — convenience entrypoint that reuses the process-level graph and runs one document.
  - **Judge gating**: for grounded runs with deterministic field scoring, `_emit_pipeline_result` suppresses the `pipeline-result` generation when the verdict is unambiguous (see `field_scoring.type_bands` in `taxonomy.yaml`) — saving both LLM-as-judge evaluator calls.
- Conditional edges are declared once in `route_spec.py` (`ROUTE_SPECS`: each node's router from `routing.py` and its destination map; `PLAIN_EDGES`), and `wire_edges` adds them after checking that every destination exists and each router's `Literal` return annotation matches its map. For example `after_classify` can return `"classify"` (transient self-loop), `"retry_classify"`, `"review_classify"`, `"extract"` or `"human_review"`. `add_route_rule` lets a rule refine a node's route; see the module docstring for the contract. **Decision points** (`decision_points.py`) are the pluggable way to use one: taxonomy `decision_points:` (empty by default) binds a node to a backend registered with `register_decision_point_backend`; the node's wrapper consults it after the node returns and stores `decision_outcomes[slot]`, and a `decision_point:<slot>` rule applies the backend's `route_hint` within the slot's `allow`, `min_confidence` and `by_class` policy (never over a park, a self-loop or a proposed verification gate: `retry_extract`, `judge_verify`, `arbiter` or `boss_escalation`, so on the extraction side a hint can add scrutiny but never remove it (classification routing stays open to hints, as `allow` says); `on_failure: fail_open | fail_safe`). With no slot nothing is wrapped or registered. Each slot may redirect a document to a non-park destination at most `max_overrides` times (default 1; state key `decision_override_counts`), and the active slots' budgets total at most 8, so hint cycles end whatever the topology. `backend:` is a registered name or an import spec `module.path:callable` (validated and imported at startup by `Watcher.start()` and the API lifespan, and again by `build_graph()`; a bad section or import error stops the process, an unregistered bare name only logs `decision_point_backend_unregistered` once per slot). Limits for now: one stalled-thread cap (4 timed-out calls still running; healthy in-flight calls never count) shared by all slots, consult time counts inside the node's `_bounded` budget, and there is no circuit breaker yet (a later phase). The classification reviewer (`agents/decision_backend.py`) and the extraction gate (`agents/extraction_gate.py`) are unchanged and will be moved onto slots later.
- Architecture doc: `docs/architecture.md`.
