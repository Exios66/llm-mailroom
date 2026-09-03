# llm-mailroom Notebooks — the Formal Plan

Status: **SHIPPED 00–13** (this document is the plan of record; 00–08 shipped
under KANBAN-095, 09–13 extend the suite to every specialist, edge cases,
Lucius-Morningstar Hub corpora, LegalBench, and vision ingestion).

Audience: anyone who wants to *see* the mailroom think — how the 13-node
LangGraph pipeline routes a legal document through its specialist agents,
where the retry/review/judge/arbiter dynamics kick in, and what every agent
contributes to the final output — without an API key, without Langfuse, and
without reading the whole graph wiring first.

## Principles (inherited from `dataset_browser`, KANBAN-078)

1. **Thin notebook + reusable module.** Each notebook `NN_<name>.ipynb` pairs
   with a module `notebooks/<name>_lab.py` holding every function. The module
   is importable and testable with only the core install; the notebook is a
   narrated walkthrough over it.
2. **Real machinery, deterministic fuel.** Notebooks run the REAL graph
   (`graph.build_graph.build_graph()`), REAL routing (`graph/routing.py`
   thresholds), REAL bins/catalog/archive filesystem outputs — driven by the
   SAME network-free mock seam the test suite uses
   (`src/tests/conftest.py`: `FakeLangChainLLM` + mocked OpenAI client).
   Nothing canned is invented for the notebooks; every output shown is what
   the pipeline actually produced.
3. **Network-free by default, real-LLM opt-in.** Mock mode is the default and
   the only mode exercised by the guard tests. Where a notebook shows a real
   LLM path, it is a clearly-marked optional cell gated on
   `OPENROUTER_API_KEY` being set — never executed in CI, never required to
   reach the last cell.
4. **Kernel-cwd-proof bootstrap.** First code cell walks up from `Path.cwd()`
   to the repo root (pyproject.toml marker), inserts it on `sys.path`, and
   fails loudly if wrong — same as `dataset_browser.ipynb`. Notebooks must
   execute headlessly from ANY cwd, including from inside `notebooks/`.
5. **Honesty labels.** Every notebook states up front: which parts are
   deterministic mocks, which are real code paths, and what a real run would
   change (latency, nondeterminism, Langfuse traces). No notebook claims a
   mock output is a model output.
6. **Sandboxed filesystem.** All runs redirect `MAILROOM_BASE_DIR` to a
   temp directory (pattern: `conftest.temp_base_dir`); notebooks never touch
   the developer's real `pipeline/` bins or `data/mailroom.db`.

## The suite

| # | Notebook | Pipeline question it answers |
|---|----------|------------------------------|
| 00 | `pipeline_anatomy.ipynb` | What is the graph? (static map) |
| 01 | `happy_path_run.ipynb` | What happens on a clean run? (dynamic) |
| 02 | `routing_dynamics.ipynb` | How do confidence bands steer the graph? |
| 03 | `review_lanes.ipynb` | How do the sorter_reviewer / judge / arbiter lanes interact? |
| 04 | `human_in_the_loop.ipynb` | How does a human decision re-enter the graph? |
| 05 | `failure_recovery.ipynb` | How do transient errors and retries behave? |
| 06 | `outputs_and_audit.ipynb` | What does the pipeline leave behind? |
| 07 | `multi_document_matters.ipynb` | How do runs group into matters/sessions? |
| 08 | `observability_traces.ipynb` | What does Langfuse see? (opt-in) |
| 09 | `all_specialists.ipynb` | One happy-path run per class (all 7 specialists) |
| 10 | `edge_cases.ipynb` | Guards, $0 amounts, unknown type, schema-invalid extract, Boss conflict |
| 11 | `huggingface_corpora.ipynb` | Navigate Lucius-Morningstar Hub datasets (offline snapshot + live opt-in) |
| 12 | `legalbench.ipynb` | LegalBench eval suite beside the pipeline (mock on a mini CUAD fixture) |
| 13 | `vision_ingestion.ipynb` | Additive page-image render path (no LLM call) |

All fourteen walkthroughs share `notebooks/pipeline_lab.py` ("the lab
bench"); 11 also uses `huggingface_lab.py` and 12 uses `legalbench_lab.py`.

### 00 — `pipeline_anatomy.ipynb` (static map)

- **Goal:** the mental model before any run: 13 nodes, entry routing, the
  conditional-edge map, the 15-agent roster and which node each agent powers,
  the 7 doc classes and their specialists + extraction schemas.
- **Content:** render the node/edge table from the LIVE graph object and
  routing module (imported, not copy-pasted); taxonomy table rendered from
  `src/config/taxonomy.yaml` via `PipelineSchema`; per-class field-type table
  (entity_list / money / date / id …).
- **Agents shown:** all 15 (roster view); no LLM calls.
- **Module surface:** `pipeline_lab.graph_map()` → nodes, conditional edges,
  agent→node map; `pipeline_lab.taxonomy_table()`.

### 01 — `happy_path_run.ipynb` (the example pipeline run) ★ Jack's ask

- **Goal:** one contract document, end to end: intake → classify (high
  confidence) → extract → judge gate skipped (above band) → compile →
  catalog → archive. Every agent's input, output, and role narrated at its
  station.
- **Content:** run the real graph under the mock seam with a high-confidence
  classification; walk the returned final state field by field; per-node
  narration table (node → agent → what it read → what it wrote → why the
  router went WHERE it went next); show the archived manifest + catalog row +
  report file that the run produced in the sandbox.
- **Dynamics shown:** the "boring" path is the baseline that makes the
  interesting lanes legible; `judge_gate` NOT firing is itself the lesson
  (band math shown explicitly).
- **Module surface:** `pipeline_lab.run_document(text, *, classification=…,
  extraction=…, matter_id=…)` → `(final_state, step_log)` where `step_log`
  is captured via a LangGraph `stream()` over the same graph (node name +
  state delta per step); `pipeline_lab.state_diff(before, after)`.

### 02 — `routing_dynamics.ipynb`

- **Goal:** the confidence-band state machine, demonstrated by re-running the
  SAME document at different mock confidence levels and comparing the paths.
- **Content:** matrix run: classification confidence ∈ {0.98, 0.80, 0.60,
  0.30, 0.10} → path each took, rendered as a step-log comparison; the
  retry_classify self-loop (low band → one retry at higher temperature
  context); medium-band survival rules (KANBAN-062 lane A entry); extraction
  confidence bands vs `judge_band_high` (0.85) gate; where each threshold
  literal lives in `taxonomy.yaml`.
- **Agents shown:** sorter, sorter_reviewer (lane entry), judge (gate entry).
- **Module surface:** `pipeline_lab.path_for(confidence)` → list of node
  names; `pipeline_lab.band_report()` → the threshold table.

### 03 — `review_lanes.ipynb`

- **Goal:** the two quality lanes and their interplay: Lane A
  (sorter_reviewer second opinion on medium-band classification) and Lane B
  (judge_verify on ambiguous-band extraction → arbiter on failed verdict →
  bounded arbiter-driven re-extraction).
- **Content:** scenario runs: (a) medium classification where reviewer
  AGREES; (b) reviewer OVERRIDES (mock returns a different doc_type — watch
  the specialist handoff change); (c) extraction at 0.80 → judge fires,
  verdict pass; (d) judge fails → arbiter approves → retry_extract once →
  arbiter_retry_count bound; (e) arbiter escalates → boss. State fields
  tracked: `review_verdict`, `judge_verdict/score/findings`,
  `arbiter_decision/reasoning/handoff`, `arbiter_retry_count`.
- **Agents shown:** sorter_reviewer, judge, arbiter, boss (escalation side).
- **Module surface:** `pipeline_lab.run_scenario(name)` presets + the same
  `run_document` core.

### 04 — `human_in_the_loop.ipynb`

- **Goal:** the `human_review` siding: why documents land there
  (low-confidence classification after retries, failed extraction, boss
  routing), what the pending state looks like on disk (review bin), and how
  a decision re-enters the graph (approve/reject → `after_human_review` →
  compile vs failed).
- **Content:** run to the review siding; render the pending review record;
  resume the SAME thread (checkpointer/thread_id) with `review_decision` set,
  both branches; contrast terminal states + audit trail rows.
- **Module surface:** `pipeline_lab.run_to_review(text)` → paused state;
  `pipeline_lab.resume_with_decision(state, decision)`.

### 05 — `failure_recovery.ipynb`

- **Goal:** resilience machinery: transient provider errors (connection
  blips) vs confidence retries — the `transient_error`/`transient_retries`
  self-loop NOT consuming the classification/extraction retry budgets; the
  `run_deadline`/`run_aborted` bounded-run guard; the failed bin as the
  honest terminal state.
- **Content:** mock client that raises `ConnectionError` twice then succeeds
  → watch the self-loop recover with budget intact; a run that exhausts
  every budget → failed bin + `error_message` + tombstone; retry the same
  doc_id (deterministic id) as attempt 2 (`run_attempt` tag).
- **Module surface:** `pipeline_lab.flaky_client(fail_times)`; scenario
  presets.

### 06 — `outputs_and_audit.ipynb`

- **Goal:** the paper trail: per-run manifest (JSON), SQLite catalog row
  (`data/mailroom.db`), the compiled report, archive bin layout, and the
  audit log — i.e., everything The-Mailroom visualizer reads.
- **Content:** after a happy-path run, open each artifact read-only and
  narrate its schema; show how `doc_id`/`trace_id`/`matter_id` thread
  through all of them; point at the The-Mailroom fields each value feeds
  (conveyor stage, review siding, metrics).
- **Module surface:** `pipeline_lab.artifacts(base_dir)` → dict of parsed
  artifacts.

### 07 — `multi_document_matters.ipynb`

- **Goal:** matter/session grouping: several documents through one
  `matter_id`, how the catalog groups them, and how the mock seam can serve
  per-document classifications in one batch run.
- **Content:** 3-document matter (contract + correspondence + court
  opinion); per-doc paths; the matter-level rollup the catalog enables;
  where Langfuse `session_id = matter_id` comes from.
- **Module surface:** `pipeline_lab.run_matter(docs)` → per-doc results +
  rollup.

### 08 — `observability_traces.ipynb` (opt-in, never required)

- **Goal:** what the same run looks like to Langfuse: trace tree, span
  naming (`verb-first node spans`), generations, scores, `langfuse_prompt=`
  links — the contract The-Mailroom renders.
- **Content:** DEFAULT: offline walkthrough of the trace contract using the
  test-suite fake fixtures (`tests/fake_langfuse.py`-style shapes) so the
  notebook still teaches the shape network-free. OPTIONAL cell: with real
  Langfuse keys set + `OBSERVABILITY_PROVIDER=langfuse`, run one mock-LLM
  (traced) document and fetch its own trace back fresh.
- **Honesty:** the offline section explicitly says "shape from fixtures, not
  a live trace".

## Shared infrastructure — `notebooks/pipeline_lab.py`

One module behind all eight notebooks (single import surface, one place to
keep honest):

- `LabSandbox` context manager: temp `MAILROOM_BASE_DIR`, `ensure_dirs`,
  `OBSERVABILITY_PROVIDER=none`, mock seam installation
  (mirrors `src/tests/conftest.py` — including the two-path mock: legacy
  `agents/*` via `llm.client.OpenAI` patch + vendored `langchain_agents` via
  `BaseAgent.llm`), env restore on exit.
- Tunable fake knobs (delegating to `langchain_agents.mock.FakeLangChainLLM`):
  `classification` dict (doc_type / contract_subtype / confidence /
  reasoning), `extraction` dict (fields + confidence), optional raise-on-N
  transient failures.
- `run_document(...)` / `run_to_review(...)` / `resume_with_decision(...)`
  built on `graph.build_graph()` + `stream(step)` step-log capture.
- Display helpers: `state_diff`, `path_of(step_log)`, `band_report`,
  markdown table renderers that degrade to plain text without ipywidgets
  (same degrade rule as `dataset_browser.py`).
- NO new thresholds, NO duplicated routing logic — every band number is read
  from `PipelineSchema.load()` / `graph.routing` at runtime so the notebooks
  can never drift from the graph.

## Conventions

- Numbering `00–08` with the leading zero; names are nouns, not verbs.
- Every notebook: title cell → "What you'll see" → honesty-label cell →
  bootstrap cell → content → "Where to go next" cell linking sibling
  notebooks.
- Every module function has a docstring naming its real-source counterpart
  (which test fixture / conftest pattern it mirrors).
- Notebooks are committed WITH stored outputs (they are the documentation);
  outputs must be reproducible headlessly (guards re-execute and compare
  cell error status, not pixel output).
- No cell may require network, an API key, or a display server to reach the
  final cell (exception: the clearly-marked opt-in cells in 08).

## Guards (per the KANBAN-078 precedent)

New `src/tests/test_notebook_suite.py`:

1. Every notebook exists, has expected title/honesty cells, and its module
   imports network-free.
2. Headless execution of every notebook via `nbclient` from a hostile cwd
   (repo root AND `notebooks/` itself) with stored outputs regenerating
   cleanly.
3. `pipeline_lab` unit pins: band math matches `graph.routing` literals;
   sandbox restores env; step-log capture returns node names in graph order.
4. No notebook cell text contains an API key pattern or performs
   `requests`/`httpx`/`openai` network calls at exec time (AST scan of the
   lab module + grep of notebook sources, opt-in cells excepted by marker).

## Build order

1. `pipeline_lab.py` + guards skeleton (the bench first).
2. 01 happy path (Jack's headline ask) → 00 anatomy (map benefits from a
   worked example existing).
3. 02 routing dynamics → 03 review lanes (the two dynamics notebooks).
4. 04 human-in-the-loop → 05 failure recovery.
5. 06 outputs & audit → 07 matters → 08 observability.
6. `notebooks/README.md` updated to scope the full suite alongside the
   dataset browser.

Each step ships independently (green suite, changelog entry) — the suite
grows one governed commit at a time, never a big-bang dump.

## Out of scope / honest gaps

- Real-LLM notebook runs are NOT part of any guard (cost + nondeterminism);
  08's live Langfuse cell and 11's live Hub cell are the only network paths
  and are marker-gated (`NB-OPT-IN-NETWORK`).
- Vision **rendering** is executed in 13 (PyMuPDF data-URIs); multimodal LLM
  calls are not (they would spend tokens). The additive contract is shown,
  not a live vision completion.
- The TUI (M4) gets notebooks only after it exists.
