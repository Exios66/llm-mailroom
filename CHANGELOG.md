# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Intake clerk + HF docclass pilot runner (The-Mailroom production contract).** Procedural `agents/intake.py` (`deterministic_normalize` / `looks_messy`, byte-compatible with The-Mailroom's `mailroom_ui/intake_normalize.py`) runs after transcription and emits verb-first span `normalize-intake` under ingest. New `scripts/run_hf_pilot.py` (`--check` / `--mock` / `--real`) loads a stratified `Lucius-Morningstar/docclass-merged` subset, traces each doc as `document-pipeline` under session `pilot-hf-<UTC stamp>` with tags `mailroom`/`pilot`/`source-docclass-merged`, and writes `data/hf_pilot/<stamp>/report.json` for The-Mailroom `eval_pipeline.py`. Ground truth (`expected_hf_class`, `expected_doc_class`, `expected_subclass`) is on trace input and metadata. Reference production session: `pilot-hf-20260825T044207Z`.

- **HF eval scale + LLM-as-a-judge overlay.** `run_hf_pilot.py` defaults to a unique `matter_id` per document (opt in to the old shared-matter Boss-conflict path with `--shared-matter`), writes `report.json` after every sample, and records exact/aligned/subclass accuracy plus cost/token/call metrics. `run_quality_judges.py --hf-report` / `--hf-latest N` runs the classification/completeness/correctness judges on those reports and attaches scores to the existing `document-pipeline` traces. Contracts handoff + the docclass contracts specialist now carry CUAD-family clause extraction and MAUD consideration-token instructions.

- **Docclass runtime arm + merger extract alias.** `MAILROOM_DOCCLASS_PROMPTS=1` (or `run_hf_pilot.py --docclass`) fetches namespaced `mailroom-docclass-<key>` prompts for every classification-chain agent, with the in-repo KANBAN-090 append as fallback. Production `mailroom-<agent>` templates stay untouched. Extract alias `merger_agreement` → `contract` lets the sorter emit the HF/MAUD label while the contracts specialist extracts; `state["doc_type"]` stays `merger_agreement` so exact HF accuracy can score 1.0. Retired `court_opinion` / `due_diligence` still park. Traces pick up a `docclass-prompts` tag when the arm is on.

### Changed

- Sorter and sorter_reviewer `reasoning_effort` is `none` so Qwen 3.7-Flash reserves the completion budget for JSON (production HF runs hit `LengthFinishReasonError` with medium reasoning).
- Langfuse score transport aliases `extraction_overall_verified_precision` → `extraction_verified_precision` (35-character config name limit).

### Fixed

- Insurance-claim schema accepts `adjuster: null` (CMS / DE-SynPUF rows have no named adjuster). JSON schema type is `["string", "null"]` so a valid extract no longer fails `schema_valid` and parks in REVIEW.

## [v0.5.0] - 2026-08-25

Minor release covering everything since v0.4.1. Live pipeline is **five document classes** (`contract`, `corporate_record`, `correspondence`, `compliance_filing`, `insurance_claim`) with FastAPI `/v1`, a 00–13 notebook suite, production prompt doctrine, and a routing-flow audit so `unknown` / retired types never extract on a nearby specialist. Full suite **539 passed**, 1 skipped.

### Added

- **API v1.** Documented `/v1` layout is live and aliases the existing handlers: `GET /v1/health`, `POST /v1/upload`, `GET /v1/queue`, `POST /v1/review/{doc_id}/resolve`, `GET /v1/status/{doc_id}`, `GET /v1/matters/{matter_id}`, `GET /v1/audit/{doc_id}`, `GET /v1/ops/status`, plus `/v1/ops/sweep` and `/v1/ops/resume`. Unversioned routes remain for the deprecation window. `GET /matters/{matter_id}` now requires the bearer token like the other management endpoints.

- **Production prompt mutation + docclass connection:** iterative, predecessor-preserving prompt mutation for every specialist and supporting LLM role, then re-derived the KANBAN-090 docclass arm from those same production bases.
  - New `src/llm/prompt_doctrine.py` holds the smallest testable production rules (unknown-type honesty, required CUAD subtype, numeric zero is a value, additive vision, same-class Boss conflicts, schema-field inventories). Frozen `*_V0` / `sorter_v12` / `contracts_specialist_v31` texts are unchanged; new production versions are **pure appends**.
  - Vendored lineage: `SORTER_PROMPT_V14` (from V12, the actual classify-node pin — not V13, which dropped CUAD subtypes) and `CONTRACTS_SPECIALIST_PROMPT_V32` (from V31). Mailroom wrappers default to those keys.
  - Production Langfuse surface grown 13 → **16** agent-name templates: added `insurance_claims_specialist`, `sorter_reviewer`, and `arbiter` (they already called `get_managed_prompt` but were never synced).
  - Docclass variants (now **14**, adding `sorter_docclass_v0`) are derived from `prompt_templates()` so `variant.startswith(production_base)` cannot drift. Contracts docclass no longer appends the stale unversioned v0 extraction prompt. Opt-in sync remains `scripts/sync_prompts.py --docclass` under `mailroom-docclass-<key>`; runtime still does not fetch docclass by default.

- **Notebook suite 09–13 — specialists, edge cases, Lucius-Morningstar Hub, LegalBench, vision:** extends the KANBAN-095 walkthroughs so the suite exhibits every specialist, remaining composed-path edge cases, and the published Hugging Face corpora. New notebooks (thin + reusable modules, stored outputs, same network-free mock seam): `09_all_specialists` (one happy-path run per taxonomy class), `10_edge_cases` (unknown type / missing CUAD subtype / $0 claim / schema-invalid extract / same-class Boss conflict / mixed-class shared field names), `11_huggingface_corpora` (Dataset Viewer snapshot of all 7 `Lucius-Morningstar/*` datasets + substring search/filter + one Hub row fed into the pipeline; live Hub refresh behind `NB-OPT-IN-NETWORK` / `MAILROOM_HF_LIVE=1`), `12_legalbench` (in-repo eval suite on a miniature CUAD fixture; honest about the Hub `legalbench-full` stub), `13_vision_ingestion` (real PyMuPDF page-image data-URIs, additive vision contract, no LLM call). Labs: `huggingface_lab.py`, `legalbench_lab.py`, `pipeline_lab.CLASS_PACKS` / `run_all_classes`. Guards in `test_notebook_suite.py` + `test_notebook_extension_labs.py`.

- **Notebook suite 00–08 implemented + guard suite (KANBAN-095, mailroom #15):** all nine planned walkthroughs from `notebooks/PLAN.md` now exist with stored, headlessly-reproducible outputs — `00_pipeline_anatomy` (static map), `01_happy_path_run` (the example run, ★ Jack's ask), `02_routing_dynamics` (one document through five confidence bands; medium-band retry + Lane A second opinion demonstrated live), `03_review_lanes` (Lane A agree/override fail-safe semantics; judge gate strictly below 0.85; Lane B arbitration incl. a **demonstrated composed-path trap**: an approving arbiter sets `arbiter_retry_count=1` while `after_retry_extraction_gated` demands `< 1`, so approved re-extractions escalate to humans instead of firing `retry_extract` — both halves are unit-pinned green separately, the composition dead-ends; flagged for a graph ticket rather than smoothed over), `04_human_in_the_loop` (park → review-bin on disk → approve resumes the SAME checkpointer thread to archived / reject is terminal into the failed bin), `05_failure_recovery` (genuine `openai.APIConnectionError`s drive the real retry classifier: belt ×3 per node entry absorbs blips, graph self-loop engages at 5, confidence budget provably untouched — L-13 empirically), `06_outputs_and_audit` (manifest/catalog/archive/audit surfaces and which visualizer stage eats which field), `07_multi_document_matters` (three-class matter under one `matter_id` + catalog rollup), `08_observability_traces` (trace identity via the pipeline's own seeding functions, offline by default, live cell behind the `NB-OPT-IN-NETWORK` marker). Shared bench `notebooks/pipeline_lab.py` extended: sequence-scripting for the legacy client seam (lists pop per call) and a flaky seam raising genuine provider-shaped exceptions. New `src/tests/test_notebook_suite.py` enforces the four PLAN guard duties (existence/title/honesty cells; nbclient re-execution from BOTH PLAN cwds matching committed outputs; lab-vs-routing threshold pins + sandbox env restoration — which caught and fixed a real double-open leak in `LabSandbox`; AST secret/network scans). `notebooks/README.md` rescoped to the shipped suite. Full suite executed green: 101 cells across 02–08, zero error cells.

- **`.env.example` condensed + environment-variable guide linked (human directive 2026-08-24, no-card edit):** template reorganized into labeled sections (LLM provider → local/offline providers incl. the Modal-vLLM flip → database → trace sinks → logging → pipeline → API security → LegalBench); every knob preserved, dead weight trimmed, and the KANBAN-064 cross-repo contract kept verbatim. New **Environment variables** section in `docs/configuration.md` closes a pre-existing honesty gap (`docs/README.md` already claimed the doc covered "all environment variables" — it covered none): quick-orientation table over all knob groups plus a pointer to the sibling repo's canonical per-provider/per-trace-sink guide (llm-entity-extraction `docs/configuration.md`). Capability suite **12 passed** after the edit.

- **Cross-repo Modal+vLLM contract verified against the entity sibling (KANBAN-096, [#16](https://github.com/Exios66/llm-mailroom/issues/16)):** llm-entity-extraction landed its own `deploy/modal_vllm.py` (KANBAN-096) — a sibling app speaking the SAME environment-knob contract as this repo's KANBAN-064 deployment (`MODAL_VLLM_MODEL/GPU/QUANTIZATION/MAX_MODEL_LEN/API_TOKEN` + `HF_TOKEN`, distinct app name + HF-cache volume, so one Modal workspace can host independent deployments per pipeline or a single deployment can back BOTH repos: mailroom flips via `DEFAULT_PROVIDER=vllm` + `VLLM_BASE_URL` + `VLLM_API_KEY`, the entity pipeline via `OPENROUTER_BASE_URL`). Verification-only change here — this repo's KANBAN-064 capability suite (`src/tests/test_vllm_modal_capability.py`) re-ran **12 passed** against the sibling clone beside this checkout, and its cross-repo test now proves the shared knob names live in both apps. No code changes.

- **Formal notebook suite plan — `notebooks/PLAN.md` (KANBAN-095, [#15](https://github.com/Exios66/llm-mailroom/issues/15)):** the repo's notebooks were never formalized as a plan; this is the plan of record for the Jupyter suite illustrating the pipeline's functionalities and its agent interactions/dynamics, including the directive's headline notebook: an example pipeline run through the agents showing the outputs and the role of each agent. Suite roster: `00` pipeline anatomy (static map from the LIVE graph object + taxonomy), `01` happy-path example run (real graph, per-node narration table: node → agent → read → wrote → why the router went where it went), `02` routing dynamics (same document re-run across classification-confidence levels; band math from `graph/routing.py` literals), `03` review lanes (KANBAN-062 Lane A sorter_reviewer agree/override + KANBAN-063 Lane B judge → arbiter → bounded re-extraction → boss escalation scenarios), `04` human-in-the-loop (run to the review siding, resume both branches via checkpointer thread), `05` failure recovery (transient self-loop vs confidence budgets, deadline/abort, failed-bin honesty), `06` outputs & audit (manifest/catalog/report/archive — everything The-Mailroom renders), `07` multi-document matters (`session_id = matter_id` grouping), `08` observability traces (offline trace-contract walkthrough + marker-gated opt-in live cell). Shared bench `notebooks/pipeline_lab.py` drives the REAL `build_graph()` through the test suite's network-free seam (`FakeLangChainLLM` tunable classification/extraction + mocked OpenAI client + sandboxed `MAILROOM_BASE_DIR`) so every output shown is what the pipeline actually produced; honesty labels, kernel-cwd-proof bootstrap, KANBAN-078-style guards (hostile-cwd headless `nbclient` execution), and a one-commit-at-a-time build order (bench → 01 → 00 → 02/03 → 04/05 → 06/07/08). Docs-only; zero behavior change.

- **Docile-style dataset browser under a new `notebooks/` folder (KANBAN-078):** thin `notebooks/dataset_browser.ipynb` over a reusable `notebooks/dataset_browser.py` module, following [rossumai/docile](https://github.com/rossumai/docile)'s `tools/dataset_browser.ipynb` pattern (thin notebook + real tool module). Browses the pilot sample set with ground truth from `docs/examples/samples/manifest.csv` (30 rows: expected class/stage/tier, ground-truth field JSON, provenance rule CUAD|external = REAL committed legal document vs synthetic) joined **read-only** with the pipeline catalog (`data/mailroom.db`) when present — per-sample overlay of stage actually reached, confidences, extracted payload, model/prompt-version/cost/latency provenance; degrades honestly when samples aren't materialized or the DB is absent/schema-less (URI `mode=ro`, never creates or mutates). Interactive ipywidgets picker behind a new optional `[notebooks]` extra (`ipywidgets>=8.1`, `jupyterlab>=4.0`); plain-text listing + detail fallback with only the core install. Notebook bootstrap walks up to the repo root so kernel cwd doesn't matter; PDF first-page text preview reuses pdfplumber (already a pipeline dep); HTML renderer escapes manifest-sourced markup. No network, no LLM calls anywhere. Tests: 10 network-free pins in `src/tests/test_dataset_browser.py` (manifest/provenance, missing+schemaless+populated read-only catalog join, summary numbers, text table, escaping, never-raise PDF text, text-mode browser, nbformat validity). Full suite **401 passed**, up from 391.

- **`insurance_claim` as a first-class mailroom document class (KANBAN-067, [#32](https://github.com/Exios66/llm-entity-extraction/issues/32)):** the seventh extraction class, integrated at every surface `court_opinion` touches — `InsuranceClaimExtraction` schema (14 fields: claim/policy numbers, insurer, insured party, claim type, dates of loss/filing, claimed amount, adjuster, damages narrative, coverage determination, denial reasons, supporting documents) registered in `EXTRACTION_SCHEMAS`; taxonomy doc_class + `insurance_claims_specialist` agent block; a full `InsuranceClaimsSpecialist` BaseAgent (managed-prompt resolution with local fallback, strict-JSON structured output, 0.3-confidence `_parse_error` lane); graph dispatch + `_extract_insurance_claims` node; classifier vocabulary and sorter `DOC_CLASSES` widened to 7; **derived prompt versions with frozen predecessors** — `SORTER_PROMPT_V13` (text path, derived from V0 = the production alias target, deliberately NOT from V12 to avoid smuggling un-promoted experimental rules into production) and `SORTER_VISION_PROMPT_V1` (image path, insurance check inserted as #5 ahead of generic correspondence per specific-before-generic ordering, subsequent checks renumbered, scratchpad widened to checks 1–7); synthetic FNOL fixture + conftest fixture; validate_pipeline/sync_evaluators enumeration updates. Honest gap: insurance_claim has NO external benchmark corpus yet (CUAD→contract, MAUD→merger, Pile-of-Law→court_opinion all have sources; claims do not — CMS DE-SynPUF is the candidate source, EDA pending), so samples are synthetic-only by design. Tests: 15 new network-free pins (schema round-trip, taxonomy shape, dispatch wiring, vocab counts, prompt-derivation discipline incl. predecessor-immutability assertions, specialist happy/parse-error paths, fixture wiring). Full suite: **391 passed**, up from 376.

- **Vendored Graphify agent skill → `.opencode/skills/graphify/` (KANBAN-065, [#30](https://github.com/Exios66/llm-entity-extraction/issues/30)):** the official opencode agent skill from [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) (upstream `v8` @ `b2cd362`, Apache-2.0/MIT) copied verbatim — `SKILL.md` + 8 `references/` sidecars — with a PROVENANCE.md (source ref, license, re-sync steps). Future-use tooling for any coding agent working in this repo: deterministic knowledge-graph build/query over the codebase, no runtime dependency until the `graphifyy` CLI is actually installed. Network-free consistency tests (`src/tests/test_graphify_skill.py`, 5 passed) pin the structure and keep the llm-entity-extraction copy byte-identical.

### Changed

- **Project renamed Mailroom → LLM-Mailroom.** Package, README, and docs branding updated to the LLM-Mailroom name.

- **README banner** swapped for the corrected pixel-art postmaster owl.

- **Dependabot `npm_and_yarn` group bump (#14)** across 3 directories (1 update).

- **Sister-repos: `llm-entity-extraction-graph` companion site (KANBAN-080).** Cross-repo link wiring in `docs/sister-repos.md`.

- **validate_pipeline.py FIXTURE_EXPECTATIONS keys corrected — intrinsic fixture expectations revived (KANBAN-085, [#42](https://github.com/Exios66/llm-entity-extraction/issues/42)):** every key in the standalone-fixture expectation map said `tests/fixtures/…` / `examples/sources/…` while repo-root-relative truth (post root-folder consolidation) is `src/tests/fixtures/…` / `docs/examples/sources/…` — so `_expectation_for()` could never match and all 14 intrinsic (doc_class, subtype) expectations were **silently skipped** for every standalone fixture since the layout move. All 14 keys corrected to real paths; NEW network-free regression suite `src/tests/test_kanban085_fixture_expectations.py` (5 guards) makes silent rot impossible: no stale prefixes, every literal key resolves to an existing file, every `/*` glob matches ≥1 fixture, `_expectation_for()` provably engages per entry with the mapped class/subtype, registry size pinned at 14. End-to-end proof (`validate_pipeline.py --fixtures --sources`): expectations now engage — per-class accuracy populated across all 7 classes, 21/22 matched-expected. **Honest signal surfaced by the revival** (invisible while the map was dead): the evidence-based mock classifies `insurance_claim/sample_claim.txt` as `contract`/`other` (0/1) — a mock-evidence limitation, not a pipeline regression; flagged as known residue. Suite **410 passed** (405 baseline + 5 new guards).

- **The-Mailroom visualizer added to the umbrella docs (KANBAN-091, [#47](https://github.com/Exios66/llm-entity-extraction/issues/47)):** [The-Mailroom](https://github.com/Exios66/The-Mailroom) — the pixel-art visual engine rendering every pipeline run as an animated document conveyor driven SOLELY by this repo's Langfuse project — integrated into every governance/architecture documentation surface of the chained repos after recon against its actual tree (v0.2.0). Here: `docs/sister-repos.md` gains a constellation-diagram node (trace-derived envelope/badge/verdict/metric annotation), an **At-a-glance** table row ("Downstream visualizer"), and a dedicated **"The-Mailroom — the visual engine"** section covering its surfaces (`mailroom-web` :8001, `mailroom-tui`), its Langfuse-only data doctrine (demo mode seeds INTO Langfuse rather than bypassing it), its **schema-mirror duty back to this repo** (`pipeline_schema.py`/`trace_interpreter.py` must track our span names, roster, doc classes, thresholds, judge scores — new spans render as `unknown` until mirrored; `MAILROOM_TAXONOMY` live override), and its governance status (own AGENTS.md/release train/test suite/wiki; downstream observer, dependency of no family repo). Root README Umbrella table row + wiki Home umbrella paragraph updated to match. Docs-only; zero behavior change.

- **Docclass prompt variants for every classification-chain role (KANBAN-090, [#46](https://github.com/Exios66/llm-entity-extraction/issues/46)):** mirror of llm-entity-extraction's docclass prompt arm — NEW `src/langchain_agents/prompts_docclass.py` ships **13 docclass variants** (`DOCCLASS_PROMPT_VERSIONS`), one per chain role: the six `langchain_agents` specialists + insurance_claims specialist + reviewer + arbiter + boss + the judge trio (completeness / classification / correctness). Every variant is a **PURE APPEND of this repo's own production base constant** — `variant.startswith(base)` holds in full, base bytes untouched, the shared DOCCLASS ARM CONTEXT block (extended 8-class primary set incl. `insurance_claim` + `merger_agreement`, doc_subclass dimensions) and role-specific rules ride after the base's own JSON-output closer as additive context, so no anchor can drift. **Production surface provably untouched:** mailroom's Langfuse deployment is 13 agent-name-pinned templates (`mailroom-<agent_name>`); docclass text never flows through `prompt_templates()` (count pin held at 13, negative assertions on every template), and reaches Langfuse only via the new OPT-IN sync path `scripts/sync_prompts.py --docclass` under namespaced `mailroom-docclass-<key>` prompt names, content-keyed and idempotent like every other sync; runtime routes fetch nothing docclass by default. Guarded by 4 network-free tests in `src/tests/test_kanban090_docclass_prompts.py` (registry completeness, full-strict-prefix derivation with extended-class-set checks, production-surface safety, opt-in-namespaced sync wiring). Full suite green at **405 passed** (20s) — zero regressions.

- **README polish retrofit (KANBAN-089, [#45](https://github.com/Exios66/llm-entity-extraction/issues/45)):** aesthetic/enhancement pass over the root README tying the shipped docile-style structure together cleanly — an **at-a-glance facts table** under the badge row (release, runtime, agents/classes, storage, observability, docs, license status); a green **release badge** (`v0.4.1`, matching pyproject + tag); two **GitHub alert callouts** replacing plain blockquotes where operational truth matters (`[!NOTE]` for the zero-DB SQLite quick-start fact; `[!IMPORTANT]` for the taxonomy-cache gotcha — edits to `taxonomy.yaml` need a watcher/API restart, carried over from AGENTS.md so README readers hit it too); three **`<details>` toggle sections** collapsing long operational blocks while keeping them one click away (config cookbook, the curated Ollama shortlist, the full bring-up runbook); and **section dividers** between the four major parts (Getting started / Architecture / Operations / Evaluation & ecosystem). Honesty rules held: no license or CI badges (repo carries neither a LICENSE file nor workflows), license row states "not yet published" outright. Verified pre-push: code fences balanced (38 fence lines), all 33 internal anchors resolve under GitHub slug rules (26 unique targets), `<summary>` tags single-line closed, at-a-glance table renders 2 cells per row, and the full suite is unchanged at **401 passed** (20.2s) — docs-only, zero behavior change.

- **Docile-style README rebuild + docmd local docs renderer (KANBAN-082, [#40](https://github.com/Exios66/llm-entity-extraction/issues/40)):** root README redesigned after [rossumai/docile](https://github.com/rossumai/docile)'s README aesthetics (the repo whose dataset-browser pattern KANBAN-078 adopted) — centered pixel-art banner (`docs/assets/banner.png`), badge row, "this repository consists of" bullet list, grouped table of contents, and a NEW agent-organization mermaid map alongside the state-machine map. Truth fixes riding along: the old hierarchy diagram claimed "6 specialists" while `taxonomy.yaml` defines **15 agents across 7 document classes** (arbiter/sorter_reviewer/judge lanes now diagrammed); the API table listed `POST /ops/pause` which does not exist and omitted the real `GET /queue`; the Quick Start upload example pointed at `tests/fixtures/…` instead of the actual `src/tests/fixtures/…`; the API section now states the bearer-token guard (`MAILROOM_API_TOKEN`) per route. Embedded links to the full repo constellation (sister loop, dojo scoring pin, corpus feeds, eval sibling, derived graph sites, HF family) via a new Umbrella table sourced from `docs/sister-repos.md`. **docmd integration:** `npx @docmd/core` (Node ≥20, zero config) documented as the local docs renderer over `docs/` — live-reload dev server + one-shot static build with sidebar nav, offline search index, `llms.txt` context files, and offline mermaid; `site/` build output gitignored; verified locally (27 pages, ~4s). Bonus repair: the four vendored `openrouter-*` skill READMEs link to `README.md#installing` — the anchor never existed in the old README; the new Installation section carries it explicitly. Docs/tooling-only; zero behavior change; suite unchanged.

- **Wiki + docs currency pass (KANBAN-077, [#36](https://github.com/Exios66/llm-entity-extraction/issues/36)):** brought documentation back to the code's actual state and mapped the llm-mailroom umbrella of governed repositories. Canonical docs: `architecture.md` now documents the **13-node** graph (adds `review_classify` Lane A + `judge_verify`/`arbiter` Lane B exception lanes, KANBAN-062/063) with the real conditional-edge map from `graph/routing.py`, corrects the checkpointer story (**MemorySaver default**, SqliteSaver opt-in via `MAILROOM_CHECKPOINTER=sqlite`; Postgres remains optional *storage* only), widens the doc-class list to 7 (`insurance_claim` included); `agents.md` gains the Insurance Claims Specialist section (§8, full schema table, honest gap: no external benchmark corpus yet — DE-SynPUF candidate via claims-data-eda) and states the vendored-prompt truth (production alias `"sorter"` → `SORTER_PROMPT_V13`, contracts alias → base prompt, lineage v0–v31 in-repo); `configuration.md` adds `MAILROOM_CHECKPOINTER`, `MAILROOM_JUDGE_VERIFY`, `VLLM_API_KEY`; `testing.md` node-count fix. NEW `docs/sister-repos.md`: the umbrella map (llm-entity-extraction sister loop w/ shared board, llm-dojo-scoring pinned `@v0.7.0`, corpus feeds Enron-Evaluation-Environment + claims-data-eda, eval sibling atticus-investigation, derived site llm-mailroom-graph, Lucius-Morningstar HF family). Wiki: Home/FAQ/_Sidebar/_Footer/Getting-Started refreshed in-repo; `sync-wiki.sh` now refreshes all 7 mirrored pages from canonical `docs/` at sync time (mirror map documented in `docs/wiki/README.md`) so the wiki can't drift again. README architecture section + mermaid updated to match. Docs-only; zero behavior change.

- **Dojo scoring engine re-pinned `v0.6.0` → `v0.7.0` (KANBAN-067):** picks up doc-type-aware metric bundles (`DOC_TYPE_BUNDLES`, 8 document classes incl. `insurance_claim`), the explicit-fallback `resolve_doc_bundle()` honesty resolver, and the `insurance_claims_specialist` agent profile (23rd). Install provenance verified (`direct_url.json`: tag `v0.7.0` @ `51822bc`); full suite **391 passed**, identical to pre-pin baseline. Changelog-only; no behavior change in this repo's code.

### Removed

- **Court opinions specialist and due diligence specialist retired from the live pipeline.** `due_diligence` and `court_opinion` are no longer taxonomy classes, extraction schemas, graph dispatch arms, or managed prompts. The sorter doctrine now emits `unknown` (human review) for judicial opinions and DD checklists/memos instead of remapping them onto correspondence or contract. Source files stay on disk; the live pilot `manifest.csv` is the 22 remaining samples (15 real Atticus/CUAD + LegalBench; 7 synthetic). Frozen langchain prompt lineage (`SORTER_PROMPT_V0`–`V14`, vision V0/V1) is unchanged.

### Fixed

- **Routing-flow audit after specialist retirement — unknown token, vision remap, missing-specialist retry:**
  - Sorter structured-output enum now includes `unknown` (a routing token, not a specialist). Restricting it to the five live classes forced court opinions / DD memos onto a nearby specialist despite doctrine.
  - Vision `classify_image` / `classify_document` no longer remap invalid labels onto `correspondence` at the model's confidence (the text-path MAILROOM PATCH twin). Empty pages return `unknown` at 0.0. Explicit `<label>` tags are honored even when the label is `unknown` or retired.
  - `after_extraction` parks unsupported / non-taxonomy types for human review immediately — no `retry_extract` of a missing specialist. Extract nodes return `{_unsupported: True}` instead of a 0.3-confidence stub that burned the retry budget.
  - Graph construction asserts dispatch keys == taxonomy keys; an unmapped `specialist:` name raises. Per-agent memory no longer falls back to `contracts_specialist` for unmapped types.
  - Lane A reviewer options include `unknown`; `after_review_classify` still extracts only live taxonomy classes.
  - `validate_extraction` treats a missing schema as **invalid** (retired/hallucinated types no longer look schema-clean).
  - Classify hard-fail / empty text now label `unknown` instead of `correspondence`. Parse-error remains correspondence at 0.3 so the classify retry budget still fires.

- **Sorter remapped unknown classes onto `correspondence` at the model's confidence:** `sorter_agent.classify` coerced any `doc_type` outside `DOC_CLASS_KEYS` to `correspondence` while keeping the stated 0.98, so `after_classify`'s unknown-type arm never fired and hallucinations archived as letters. Invalid types are now preserved; parse-error remains the only correspondence default (explicitly 0.3). Missing/empty type on `after_classify` also parks for review (already true on `after_retry_classify`).
- **Mixed-class matter conflict false positive:** `_detect_conflict` compared shared schema field names (`effective_date` on both contract and corporate_record) across different document classes in the same matter and escalated to the Boss. Conflict is same-class only.

- **Pipeline logic audit — operational/composed-path defects (provider- and sink-agnostic):**
  - **Sticky `transient_error` infinite loop (Lane B judge):** `judge_verify_node` success / skip / hard-fail paths never cleared `transient_error`. After a single provider blip the flag stuck `True` on LangGraph merge, so `after_judge` self-looped the successful judge forever until the run deadline aborted a well-extracted document to the failed bin. Every judge/arbiter/boss/reviewer exit path now writes `transient_error=False` except the transient arm.
  - **Retry nodes wrote the unused `transient_retries` counter (L-13):** `retry_classify_node` / `retry_extract_node` incremented a key routing never reads. Combined with routers bouncing transients back to first-pass `classify`/`extract`, a blip during re-classification burned a confidence attempt and skipped Lane A. Retry nodes now own `transient_retries_retry_classify` / `transient_retries_retry_extract` and self-loop on the SAME node.
  - **Boss transients ignored leftover `review_decision="approved"`:** review-resume sets that flag; `after_boss` treated it as a successful ruling, so a Boss blip on a conflicted resume archived without adjudication. Boss now has a per-node transient budget + self-loop; `after_boss` checks the flag first. Arbiter gets the same L-13 treatment.
  - **`retry_classify` hard-failures crashed to the failed bin (L-15 gap):** first-pass classify was converted to human review; the retry node still `raise`d. Now parks for review.
  - **Classification guardrail did not clamp on invalid subtype:** a contract at 0.95 with a missing/unknown CUAD subtype auto-extracted. `apply_classification_guard` now clamps like extraction (ceiling 0.5). Lane A reviewer is given the CUAD subtype vocabulary (it previously received an empty list).
  - **Reporter exception failed a successful extract:** `compile_report_node` did `**None` when `extracted_data` was null and let reporter exceptions abort the run. Fallback report now preserves extracted fields and archives.
  - **Numeric `0` treated as empty extraction** (`_has_substantive_content`), so a $0 claim amount could force retry/review. Zero is a real value.
  - Arbiter `fields_to_fix` / handoff now ride on the specialist retry prompt (previously only judge findings). Per-agent memory records the actual specialist, not always `contracts_specialist`. Ingest claims via `Path.is_relative_to` (not substring). OpsMonitor `is_paused` respects pause TTL.

- **Lane B arbiter-approved re-extraction now fires — composed-path trap fixed (KANBAN-098, [#17](https://github.com/Exios66/llm-mailroom/issues/17)):** a real defect found by the KANBAN-095 notebook work and demonstrated live in `notebooks/03_review_lanes.ipynb`: an arbiter decision of `retry_extraction` could never dispatch `retry_extract` in a composed run. The approving node sets `arbiter_retry_count` to 1 **at approval time** (`graph/build_graph.py::arbiter_node` — the retrying extract node reads that count to weave the fix-list into its prompt), while `after_arbiter` demanded `< 1`, so every approved retry escalated straight to human review and the second judge pass never ran; both halves were unit-pinned green separately while the composition dead-ended. Fix: the bound is approval-INCLUSIVE (`<= 1`) — the FIRST approval dispatches to `retry_extract`; only a SECOND arbitration demanding another retry finds the budget spent and escalates. The one-retry-per-document bound itself is unchanged. New composed-path regression pins `src/tests/test_kanban098_arbiter_retry_path.py` drive the REAL graph end-to-end (approve → re-extract → re-judge → compile → archive; plus the still-bounded second-demand escalation — 3 network-free tests); the Lane-B unit pin updated to the approval-inclusive contract; notebook 03's bounded-retry narrative and stored outputs regenerated to demonstrate the FIXED path. Full suite **462 passed**.

## [v0.4.1] - 2026-08-21

### Added

- **Modal+vLLM offline serving capability (KANBAN-064) — framework-in-place:** the pipeline can now be pointed at a Modal-deployed vLLM server for local/offline operation later, with zero behavior change today (OpenRouter stays primary unless `DEFAULT_PROVIDER=vllm`). Pieces: `deploy/modal_vllm.py` (vLLM's own OpenAI-compatible `/v1` server behind Modal `web_server`; env-configured MODEL / GPU / quantization / max-model-len / required bearer token baked in at deploy time via `modal.Secret.from_local`; persistent HF-weights volume for warm restarts; `modal serve` smoke-test mode), optional `VLLM_API_KEY` bearer support in the existing `vllm` provider (keyless local servers unchanged — client falls back to `"not-needed"`), `[deploy]` install extra (deploy-time only; runtime never imports modal), `deploy/README.md` runbook (deploy → flip `.env` → flip back), `.env.example` guidance. Tests: 12 network-free tests loading the app with a stubbed modal module + exercising the provider seam (DEFAULT_PROVIDER precedence, base-URL override, keyless-vs-bearer end-to-end through `get_llm`).

## [v0.4.0] - 2026-08-21

### Added

- **Lane A — Sorter Review (KANBAN-062, #28):** medium-band classifications
  that survive the re-classification pass now receive an INDEPENDENT agent
  second opinion (`sorter_reviewer`, blind re-classification) before any
  human review. A confident reviewer resolves the ambiguity automatically
  (winning label applied to state; original sorter answer preserved in
  `reviewer_*` fields); an unsure/conflicting reviewer escalates to human
  review with BOTH opinions recorded on state. Hot path unchanged.
- **Lane B — Judge + Arbiter (KANBAN-063, #29):** gated in-pipeline
  completeness verification reusing the offline-battle-tested
  `CompletenessJudge` rubric. The judge fires ONLY for ambiguous-band
  extractions (low <= confidence < `judge_band_high`, default 0.85) — clean
  runs keep today's path with zero added LLM calls; disable the lane with
  `MAILROOM_JUDGE_VERIFY=off`. Failed verdicts go to a bounded arbiter:
  accept-with-caveats → proceed, retry_extraction (once, fix-list attached
  via handoff context) → re-extract, else human_review. All failure modes
  fail safe toward human eyes.
- New agents: `agents/sorter_reviewer.py`, `agents/arbiter.py` (profiles
  registered upstream: llm-dojo-scoring v0.6.0).
- Graph nodes + routers: `review_classify`, `judge_verify`, `arbiter`;
  `after_review_classify`, `judge_gate` + gated extraction routers,
  `after_judge`, `after_arbiter`; per-node transient budgets (L-13).
- Tests: `src/tests/test_lanes_062_063.py` (routing bands, gate cost
  contract incl. kill-switch and band edges, topology assertion, mocked-node
  behavior, fail-safe paths).

### Changed

- `test_pipeline_e2e.py::test_graph_routes_medium_confidence_to_review`
  re-pinned to Lane A behavior (confident reviewer auto-resolves the medium
  band; both opinions preserved on state).
- Dependency pin: llm-dojo-scoring @ v0.6.0 (review/audit profile registry).

## [v0.3.2] - 2026-08-21

### Changed

- **Unified scoring migration (KANBAN-061):** the 1,273-line local
  `observability/field_scoring.py` is replaced by a deprecation shim over
  [`llm-dojo-scoring`](https://github.com/Exios66/llm-dojo-scoring)
  v0.5.1 — one scoring engine for both document pipelines.
  - All public symbols re-exported from `llm_dojo_scoring.field_scoring`
  - Taxonomy wiring at import via the package's `configure(**overrides)`
    (YAML lists coerced to tuple/set, mirroring entity-extraction's
    `dojo_config.py`)
  - Mailroom-local glue kept: `get_type_bands`, `field_is_ambiguous`,
    `warm_embedding_model`; `get_field_types` auto-loads
    `config/taxonomy.yaml` (package version requires an explicit dict)

### Added

- `llm-dojo-scoring @ v0.5.1` dependency in `pyproject.toml`
- Import-time validation of `scores.py::SCORE_CONFIGS` against the dojo
  metric registry (all 37 names verified covered) — schema drift now fails
  loudly at import instead of emitting unregistered metrics
- `observability/README.md` documenting the split of ownership

### Tests

- Embedding-rescue patch targets moved to `llm_dojo_scoring.field_scoring`
  (internals live there now); full suite: 326 passed.

## [v0.3.1] - 2026-08-19

### Changed
- **`AGENTS.md`** — the tracing paragraph now documents the `auto` chain
  (`langfuse → braintrust → phoenix → none`), the new `src/observability/phoenix_setup.py`
  local cost-free Phoenix backend, and the OpenInference OpenAI instrumentation,
  matching the v0.3.0 code.

## [v0.3.0] - 2026-08-19

### Added
- **Arize Phoenix tracing backend** (`src/observability/phoenix_setup.py`),
  aligned with the `llm-entity-extraction` architecture: a local,
  cost-free OpenTelemetry-native sink. `observability/tracing.py` now
  resolves `OBSERVABILITY_PROVIDER=auto` as `langfuse` → `braintrust` →
  `phoenix` → `none`, so with no cloud keys set tracing falls through to
  local Phoenix instead of turning off silently. `instrument_openai_client`
  and `flush` route to Phoenix via the same facade; LLM calls are traced
  with the OpenInference OpenAI instrumentor.
- **`phoenix` / `arize-phoenix` + `opentelemetry` runtime deps** and the
  `postgres` optional extra (see pyproject).
- **Optional-extra dependency split** in `pyproject.toml`: the heavy
  `sentence-transformers` + `scipy` embedding/scoring stack moved out of the
  core install into `pip install -e ".[embeddings]"`; `psycopg[binary]`
  moved into `.[postgres]` (SQLite is the default). Both degrade gracefully
  when not installed.

### Removed
- **`apscheduler`** runtime dependency — no code used it (the ops monitor
  uses `asyncio.sleep`/`wait_for`).
- **`sentence-transformers` / `scipy` from the core install** — now optional;
  dropped the ~500MB torch chain from a default install.

### Changed
- **`observability/tracing.py`** docstring + `resolve_provider_name`:
  the `auto` chain now falls back to local Phoenix (cost-free) before
  `none`, matching `llm-entity-extraction`'s local-first fallback.
- **`.env.example`** — documented the new `auto` chain and added the
  `PHOENIX_TRACING` / `PHOENIX_ENDPOINT` / `PHOENIX_SERVICE_NAME` /
  `PHOENIX_PROJECT` knobs.
- **`docs/configuration.md` / `docs/deployment.md`** — documented Phoenix,
  the `auto` fallback, and troubleshooting for post-cloud-key tracing.
- **`src/tests/test_observability.py`** — the no-key default now asserts
  `phoenix` (with `PHOENIX_TRACING=disabled` → `none`), and explicit
  `phoenix` provider selection is covered.

## [Unreleased]

### Added
- **Braintrust + OpenRouter skills for all agents**: `braintrust`
  (github.com/braintrustdata/braintrust-skills — the agent-auto-improvement
  loop: production traces -> failure taxonomy -> dataset -> scorers ->
  offline evals) and `openrouter-models` / `openrouter-generations` /
  `openrouter-analytics` / `openrouter-benchmarks`
  (github.com/OpenRouterTeam/skills — catalog/pricing/provider latency,
  per-generation cost/latency debugging, spend analytics, benchmark data)
  join the langfuse/langchain/langgraph skill stack under
  `.opencode/skills/`. AGENTS.md documents the full set.
- **Langfuse best-practices compliance verified** against the langfuse
  instrumentation baseline: model/tokens on every generation, stable trace
  naming, verb-first spans + correct observation types, curated (PII-masked)
  IO, sessions, tag taxonomy, environments, prompt linking, score configs,
  evaluators, dashboards, flush health, and the self-audit loop — on langfuse
  4.14.3 with the v4 API surface.
- **LangGraph + Langfuse best-practices audit of the processing pipeline**
  (`docs/reports/audits/2026-08-12-langgraph-langfuse-best-practices-audit-of-the-processing-pipeline.md`):
  skill-grounded review of every agent's behavior when running the full
  pipeline. Verified compliant: attempt-scoped `thread_id` + checkpointer
  selection, partial-update state nodes, conditional routing with explicit
  END, deadline/budget-guarded transient-retry loop (a documented superset
  of native `RetryPolicy`), manifest-based human review (deliberate
  durability pattern vs native `interrupt()`), and the Langfuse scaffold
  (deterministic trace ids, sessions, tags, environments, verb-first spans,
  auto-created score configs) — the native LangChain/LangGraph callback
  handler cannot provide those, so the hand-rolled layer is not a
  reinvention.
- **Native LangGraph RunnableConfig tags/metadata on the graph invoke**:
  `graph.build_graph.run_pipeline` now passes the same environment/run/source
  tags and pipeline/run_deadline/attempt/run_id metadata natively in the
  invoke config (in addition to the Langfuse trace), so any callback or
  LangGraph-native instrumentation sees the run's classification dimensions
  without duplication.
- **LangChain + LangGraph skills for all agents** (from
  github.com/langchain-ai/langchain-skills): langchain-fundamentals, python
  quickstart, dependencies, middleware + langgraph-fundamentals, python
  quickstart, cli, persistence, human-in-the-loop, ecosystem-primer —
  project skills under `.opencode/skills/`, alongside the existing langfuse
  skill. AGENTS.md documents the set.

### Changed

- **Repository root restructured to the essentials**: all Python code now
  lives under `src/` (agents, api, config, graph, langchain_agents,
  legalbench, llm, observability, pipeline, schemas, scripts, storage,
  tests); `examples/` moved to `docs/examples/`; `wiki/` moved to
  `docs/wiki/`. The root now holds only `src/`, `data/` (runtime state),
  `docs/`, `.opencode/` and the tooling files. Every import and expected
  path was fixed in the same change: scripts bootstrap `src/` onto
  `sys.path` (`PYTHONPATH=src` for entry points and CLIs — e.g.
  `PYTHONPATH=src python -m pipeline.watcher`), `pipeline/env.py` reads
  `.env` from the repo root, `pipeline/config.py` resolves
  `src/config/taxonomy.yaml`, pyproject maps `testpaths=["src/tests"]` +
  `pythonpath=["src", "."]`, and all data/manifest/wiki paths were
  re-pointed (`docs/examples/samples/manifest.csv`). Docker:
  `src/config/docker/docker-compose.yml`. Repository-structure maps in the
  root README, AGENTS.md, docs/, and wiki were updated to match; the
  console's `MAILROOM_TAXONOMY` documentation now points at
  `src/config/taxonomy.yaml`. No functionality changed — the full suite
  passes under the new layout.

### Added

- **LegalBench evaluation suite (`legalbench/`)**: a self-contained submodule that evaluates models through the LegalBench task families on the locally-mirrored corpora — `contract_qa` (**binary answer**: the full CUAD annotations, 510 contracts × 41 clause categories = 20,910 yes/no questions with evidence spans) and `family_classification` (**multiclass classification**: 200 labeled CUAD contract texts into the 25 contract families + `other`). Each run: deterministic local scoring (accuracy, macro per-category accuracy, yes-class F1, ECE calibration; strict + equiv family accuracy, macro-F1), one Langfuse trace per run with per-question spans and run-level scores (new `legalbench_*` score configs in `observability/scores.py`, 29 → 34), and an automatic experiment-log record appended to the shared JSONL on completion — which also regenerates the markdown log, the experiment-log site data, and the synced copy at `docs/reports/experiments/experiment_log.md`. Run via `python -m legalbench.cli --task contract_qa|family_classification --n 30 [--mock]`. The sibling repo's `scripts/site/build_site.py` gained `legalbench_binary_answer` / `legalbench_multiclass_classification` headline + breakdown + scoring-guide handlers so the site renders the new runs. Tests: `tests/test_legalbench.py` (18, synthetic corpora, network-free).
- **Full CUAD corpus support (issue #9)**: `scripts/fetch_full_cuad.py` now downloads the complete 510-contract CUAD v1 dataset (annotations, txt, PDFs, master clauses) and produces an EDA (`data/cuad/EDA.md` + `subtype_distribution.json`) that validates the mailroom 25-family `CONTRACT_SUBTYPES` taxonomy against the corpus. Subtype mapping is folder-authoritative where the CUAD PDF tree exists (198 contracts, 100% alias coverage) and title-derived elsewhere, with a delta table vs the CUAD paper's canonical counts (21/25 families exact; residual deltas are compound-title artifacts documented in the EDA).
- **Pipeline smoke-test CLI**: `scripts/validate_pipeline.py` runs the full mocked pipeline over fixture/sample/PDF files and reports per-class pass rates plus the sorter's subtype distribution (exit 0 only if every file archives).
- **Field-scoring threshold calibration (issues #4/#5)**: `scripts/calibrate_field_scoring.py` builds a labeled sample from `examples/samples/manifest.csv` ground truth — exact/format variants labeled correct, controlled perturbations (off-by-one dates, dropped list items, typo'd names, contradictory paraphrases) labeled incorrect — and reports per-field-type separation (correct vs incorrect score distributions, calibrated cutoffs, verdicts). Calibration showed date/id/money/free_text are deterministically decisive, while name and entity-list scores overlap for real errors (Jaro-Winkler/token-set are typo-tolerant by design), so those types must escalate to the LLM judge. The verdicts are encoded as new per-type `field_scoring.type_bands` overrides in `taxonomy.yaml` (`never` / calibrated `[low, high]` cutoffs / trust-only-perfect `[0.5, 1.0]`), replacing the single global `ambiguous_band` for judge gating (`field_is_ambiguous()` in `observability/field_scoring.py`).
- **Tailored Langfuse dimension dashboards (issue #2)**: `scripts/sync_dashboards.py` now syncs a third dashboard — **Mailroom Quality — Completion / Correctness / Accuracy / Latency** — with dedicated widgets for completion (stage_completed rate, expected_field_presence, judge completeness), correctness (deterministic extraction_overall_score, judge extraction_correctness, class_correct rate, guardrail rate), accuracy by doc class, duration/latency (run_duration_seconds avg + p95), cost, and LLM-call volume. Scoped to pilot runs where ground-truth scores exist.

### Changed

- **Catalog now tracks the true conveyor position of every document** (was: only happy-path runs got a record, and always with the stale `classified` stage): `ingest_node` upserts a `processing` row (so stuck-doc detection and `/ops/status` see in-flight documents), `human_review_node` upserts `review` (so `/ops/status` review_queue and error-rate stats are accurate instead of permanently 0), `catalog_write_node` writes the pre-archive row, and `archive_node` finalizes it to `archived`. `storage/catalog.py` needs no changes — the graph now writes the right rows at the right time. Regression tests in `tests/test_conveyor_stages.py`.
- **Extraction conflict adjudication is now live** (was: `conflict_detected` was never set anywhere, so the Boss in-graph escalation path was unreachable dead code and `boss_escalation_node` never passed matter context): `graph/build_graph.py:_detect_conflict` compares a fresh extraction against archived records of the same matter (normalized field comparison, list-aware) and escalates to the Boss when a populated schema field differs; `boss_escalation_node` now fetches and passes the archived matter context to `BossAgent.adjudicate` so the decision is grounded in the actual conflicting records. The mock Boss safely defaults to `review` on parse failure (never archives a conflict silently).
- **Watcher pauses cleanly and resumes automatically**: the ingestion-pause check moved inside the try/finally (the old early return leaked the file in `_active_files` forever), and a periodic inbox rescan (`WATCHER_POLL_INTERVAL_SECONDS`, default 5s) re-attempts files left in the inbox while paused once `/ops/resume` clears the flag.
- `retry_classify_node` now runs the same `guard_classification` as the first pass (out-of-range confidence / unknown doc type / invalid contract subtype from a retry previously bypassed the guard and could route straight to extraction).

### Fixed

- Empty-but-schema-valid extractions could be archived: `guard_extraction` now also fails when the extraction has no substantive content (all fields null/empty/`[]`, `_`-prefixed keys ignored), clamping confidence so routing sends the document to retry → review instead of archiving an empty extraction with high stated confidence.
- `classification_guardrail` is now recorded on state by `classify_node`/`retry_classify_node` (was never set), so `guardrail_triggered` scoring and traces reflect classification guardrail events, not just extraction ones.
- A run that crashed/aborted after ingest minted a second doc_id: `_finalize_aborted` now reuses the in-flight processing manifest's doc_id (via `_existing_processing_doc_id`), so the failed manifest/catalog record supersede the same document instead of orphaning the ingest manifest.
- `score_and_log_extraction` no longer attaches `extraction_overall_score` when no field was scored (`overall_score` is None) — Langfuse rejects null score values.

### Fixed (audit sweep 2)

- **Audit hash chaining is now wired end-to-end**: `archive_node` fetches the previous entry's hash via the (previously dead) `get_latest_audit_hash` and links the archived entry to it, so any second event for the same doc_id (review-resume re-archive, re-runs) forms a verifiable chain instead of a broken `prev_hash=""` link. Human review decisions are now audited: `POST /review/{doc_id}/resolve` appends hash-chained `review_approved` / `review_rejected` entries (actor `human_reviewer`). Verified: multi-entry chains return `chain_valid: true`.
- **DB↔Langfuse correlation fixed**: `_execute_run` captures `tracing.get_trace_id()` inside the trace block and threads it through the state so ingest/archive/review manifests and catalog records all carry the trace id (previously the column was always `NULL`).
- **Review rejection now closes the conveyor loop**: `resolve_review(rejected)` moves the file from the review bin to the failed bin and flips the catalog record to `failed` (was: manifest=failed while file stayed in review/ and the catalog said review — inflating review_queue forever).
- **`pipeline-result` generation is suppressed for non-terminal runs**: a run that ends in `review` (or `failed`) no longer emits the judge generation — the resumed (approved) run emits the single authoritative one. Fixes double evaluator calls and conflicting verdicts on the same trace for review-resumed documents.
- **Wrong-class grounded runs always reach the LLM judge**: when the sorter's `doc_type` disagrees with the expected class, `judge_required` is forced `True` — the deterministic field scorer compares against the wrong schema and would otherwise suppress the verdict for exactly the runs that need scrutiny.
- **`/ops/resume` reports accurate `was_paused`** (was hardcoded `true`).
- **Empty/unreadable documents route straight to review** without burning a classification retry (attempts set past `retry_max`), preserving the "Empty or unreadable document content" escalation reason.
- **`ImageExtractor` fixed**: its own `image_extractor` agent config in `taxonomy.yaml` (was `agent_name = "sorter"`), calls now go through `retry_chat_completion` with the run deadline and `record_usage` (was a bare `chat.completions.create` with no retry/deadline/usage accounting). Image extensions (`.jpg/.png/...`) added to `file_extensions` so the watcher actually picks up image documents.
- **Stuck-doc detection now covers `classified`**: a process kill in the catalog_write → archive window leaves the catalog at `classified` with the file in processing/ — now caught by `get_stuck_documents` (review docs are intentionally excluded; they await human action and are counted as review_queue).
- **`human_review_node` sentinel renamed `pending_review`** (was `rejected`, which leaked a false verdict into traces/manifests for docs merely awaiting review).
- **`create_trace_score` accepts `observation_id`** (matching the Langfuse SDK), un-breaking the latent field-scoring attachment path.
- **Post-invoke scoring/emission is best-effort**: a failure after the graph completes (scoring, pipeline-result emission) no longer surfaces as a pipeline failure to the watcher.
- **`/ops/status` error rates no longer carry a `None` doc_type key** (unknown-type/aborted docs are excluded from the bucket stats).
- **`/upload` gates file extensions and avoids overwriting** same-named inbox files (uniquified names).
- `asyncio.get_event_loop()` deprecation cleaned up: graph storage/audit/score helpers use a shared `_run_coro` (running-loop thread-safe scheduling or fresh `asyncio.run`); duplicate `audit_entry_written` log removed.




- **Vision is now additive — page caps never drop document content**: previously the vision path replaced the transcription with "page images attached" in the prompt only, so `vision.max_pages` silently omitted any pages beyond the cap (a 52-page contract only had its first 10 pages sent). Now the full `doc_text` is ALWAYS the message body and page images are appended on top for vision-capable models (`agents/base.py:_build_multimodal`); `llm/vision.py:render_pdf_pages` treats `cap<=0` as "render ALL pages", and `graph/build_graph.py:_render_doc_pages` propagates the configured `vision.max_pages`. Environment overrides `MAILROOM_VISION_ENABLED`/`MAILROOM_VISION_MAX_PAGES`/`MAILROOM_VISION_DPI` let pilots sweep configs without editing taxonomy.yaml. New `scripts/run_vision_sweep.py` (runs the same real docs under text-only / vision-10 / vision-all) and `scripts/write_pilot_report.py` (renders a tracked markdown+JSON report to `docs/reports/pilots/pilot-vision-tradeoff.md`, including per-doc ground-truth `expected_fields` vs extracted JSON and the deterministic field scoring for judge-auditable accuracy scoring). Measured tradeoff on the 3 real CUAD/Atticus PDFs (qwen3.7-flash, Aug 2026): text-only field score 0.533 @ 23.7k tok/doc, vision-10 0.618 @ 55.9k tok/doc, vision-all 0.628 @ 119k tok/doc — **vision-10 is the pragmatic optimum** (most of vision's benefit at ~half vision-all's tokens); vision-all only pays off for scanned/short docs. Regression tests in `tests/test_vision.py`.

- **Vision ingestion for vision-capable models**: when an input agent's model supports image input (`vision:` config in `taxonomy.yaml`, substring-matched — Qwen etc.), PDFs are rendered page-by-page to image data-URIs and sent to the sorter + all six specialist prompts as multimodal `image_url` content instead of (only) the transcribed text. `graph/build_graph.py:_render_doc_pages` (via `llm/vision.py:render_document_pages`, PyMuPDF) caps pages at `vision.max_pages` (default 10) and dpi at `vision.dpi`; `agents/base.py:_build_multimodal` builds the content list only when the agent's model is vision-capable (`_uses_vision`), so text-only models, the judge, reports, and auditability receive the unchanged plain-string `doc_text`. For scanned PDFs the expensive LLM transcription pass is skipped when the pipeline is vision-capable (`llm/vision.py:pipeline_uses_vision` — the page images carry the content; raw direct extraction still stored as `doc_text`). `NON-vision models and mock runs are completely unaffected` (mock uses model `mock-model`, never vision). Config: `vision: {enabled, max_pages, dpi, models}`, with `pymupdf` added as a dependency. Regression tests in `tests/test_vision.py`.

- **Real pilot runs are restricted to actual committed legal documents**: `run_pilot.py --real` and `run_quality_judges.py --real` now process only the real samples — the 9 Atticus/CUAD contract & agreement PDFs (`contract_01..03`, `atticus_01..06`) plus the 6 LegalBench MAUD and 6 Pile of Law external samples (21 real samples). The repo-written synthetic `.txt`-derived PDFs (compliance/corporate/correspondence/due_diligence/ambiguous, 9 samples) are **mock-only**: they exist to exercise pipeline machinery and must never spend real LLM/eval tokens or pollute live traces, so `--real` refuses them (a synthetic-only selection errors out before any document is processed). Classification lives in `scripts/prepare_samples.py:is_real_sample` (source starts with `CUAD` or `external/`); mock runs keep the full 30-sample set. Regression tests in `tests/test_real_sample_gate.py`.

- **Live runs can never fall back to mock LLM results**: `llm/providers.py:resolve_provider` now rejects the historical `mock-key` placeholder (treated like a missing key) at the choke point every entrypoint passes through — the watcher, API, ops monitor, and `--real` pilot runs all fail fast with a clear error instead of silently executing against fake credentials. `scripts/run_pilot.py` and `scripts/run_quality_judges.py` no longer default to mock mode: the `--mock`/`--real` flag is now mandatory (error if omitted), no `OPENROUTER_API_KEY=mock-key` placeholder is set at import time, and `--real` refuses to start unless a real key is present. Mock execution is now reachable only via an explicit `--mock` flag (pilot/offline harnesses and tests). Regression tests in `tests/test_mock_isolation.py`.
- **Confidence scores are now evidence-based, never anchored on 0.95**: the sorter and all six specialist prompts now require the model to derive `confidence` from the evidence in the current document — the share of schema fields actually found, fields left null, uncertain values, truncated input, and (for the sorter) competing-class signals — using the full 0.0-1.0 range and explicitly forbidding defaulting to a fixed high value (0.90/0.95). Previously four of six specialists had no confidence guidance at all and every agent defaulted to round high scores, which produced near-uniform 0.95 confidences in pilot traces. Deployed via `scripts/sync_prompts.py` (v3/v4 of `mailroom-sorter` + the six specialist prompts; regression test in `tests/test_agents/test_prompt_calibration.py`). The mock pilot also now reports a deterministic 0.95-0.99 confidence spread instead of a flat 0.95 for every sample.
- Classification routing now has a **medium-confidence band → human review**: `graph/routing.py:after_classify` uses the previously dead `confidence.high` as the auto-continue gate — `confidence >= high` proceeds to extraction, `low <= confidence < high` ("classified but not clearly confident") routes to `/review/`, and `confidence < low` keeps the retry → review flow. `confidence.high` recalibrated 0.85 → 0.95 from observed sorter behavior (pilot, Aug 2026: all 17 unambiguous docs scored ≥ 0.95 while the deliberately multi-topic `ambiguous_01` memo scored 0.90 and had been auto-archived with a correct-but-overconfident classification). `ambiguous_01` now lands in `review` as the manifest expects; docs at 0.95+ still auto-archive. Medium-confidence classifications also carry the sorter's reasoning into the review manifest (`escalation_reason` now set below `high` instead of the hardcoded 0.7). Extraction routing is unchanged (it has its own schema gate + Boss conflict escalation).
- The pipeline judge verdict is now three-way **CORRECT/PARTIAL/MISS** instead of a strict binary CORRECT/MISS: `mailroom-pipeline-judge` (`scripts/sync_evaluators.py`) issues PARTIAL when the class and stage are correct and the extraction is substantially correct but has a limited number of material gaps (including partial list coverage, which counts as a single partial gap rather than a wholly absent field). MISS is reserved for wrong class/stage, failed/aborted runs, contradicted values, or broad omission (the extraction fails to cover the majority of material expected facts). Live runs without ground truth follow the same three-way rubric against the document text. Substantially correct runs that previously earned MISS (e.g. `contract_01` with 6/10 fields correct) now earn PARTIAL; the categorical evaluator output spec is now `CORRECT/PARTIAL/MISS`, and the quality score remains independent of the verdict.
- Judge evaluator model routing is restricted to Flash models only: the OpenRouter connection now exposes `qwen/qwen3.7-flash` and `deepseek/deepseek-v4-flash`, with no Pro model. The quality evaluator uses `deepseek/deepseek-v4-flash` because Langfuse evaluator preflight rejected Qwen for this evaluator configuration; application agents may continue using Qwen.
- Added independent `mailroom-pipeline-quality` evaluator + `mailroom-pipeline-quality-rule`: grounded runs now receive a proportional 0.0-1.0 quality score alongside the run verdict. For `contract_01`, the latest run scored `MISS` plus quality `0.83`, exposing substantial correct work instead of treating the run as zero-quality.
- Judge payloads are now human-readable and non-duplicative: grounded `pipeline-result` input is a labeled pretty-printed `EXPECTED_FIELDS` block, output contains one cleaned schema-only extraction, `_report` recursive metadata is removed, and expected fields are no longer duplicated in `ground_truth`. The deployed evaluator prompt documents this exact input/output contract.
- Offline quality judging now runs classification, completeness, and correctness as three independent evaluations with fresh judge instances, separate failure boundaries, and separate score comments. Missing extraction data no longer skips all dimensions, one failed dimension no longer blocks the other two, and repeated judge iterations are preserved under `evaluation.runs` instead of overwriting history.
- Physical judge prompts are now evidence-bounded and calibrated: completeness and correctness ignore `_` metadata, accept semantic/date/list equivalence, score material fact coverage rather than list equality, respect truncated-source limits, and require concrete source-supported contradictions before alleging fabrication; classification uses only the visible document and configured taxonomy with calibrated ambiguity handling. Updated managed prompts: `mailroom-judge`, `mailroom-judge-classification`, and `mailroom-judge-correctness`.
- Grounded evaluator prompt v6 now explicitly isolates each judge call to the current document's `input`/`output`, forbids importing facts from other traces or general legal knowledge, and removes ambiguous MISS criteria that conflated omitted expected detail with fabrication. The deployed prompt contains taxonomy definitions only, not case-specific facts.
- Grounded judge calibration corrected for correspondence extraction: `correspondence_01` ground truth now includes all source-supported payment, remedy, fee, waiver, and representation facts; evaluator v5 uses semantic fact coverage for list fields instead of one-to-one list equality and does not treat omitted expected detail as fabrication. The correspondence specialist prompt now preserves every material obligation, deadline, remedy, and waiver and forbids unsupported inference. A real rerun produced complete extraction and a `CORRECT` verdict.
- Pilot runs now get a **dedicated Langfuse session** per run (not per sample): `run_pilot.py` computes `session_id = pilot-<mode>-<timestamp>` and `run_id` once in `main()`, threads them through `run_pipeline(session_id=..., run_id=...)` → `_execute_run` → `pipeline_trace(session_id=...)`. All 30 traces of a pilot run land in one session in the Sessions view. `run_id` is also recorded in trace metadata + the pilot report. Watcher/API paths are unchanged (still `matter_id`). Caveat: trace ids are deterministic per filename, so re-runs of already-traced samples keep their first run's session (same as tags/environment immutability).
- **Token churn cut ~90% for grounded pilot runs**: `graph/build_graph.py:_emit_pipeline_result` now detects when `ground_truth` carries literal per-field `expected_fields` and skips the document text in the judge input — the input becomes the small extracted-vs-expected payload (~1-3k chars vs up to 100k chars). The evaluator prompt (`scripts/sync_evaluators.py` v5) uses semantic fact coverage for list fields and ignores pipeline metadata such as `_report`. Live runs (no ground truth) keep the full-text rubric path unchanged.
- **Per-field ground truth in the manifest**: `examples/samples/manifest.csv` now has an `expected_fields` JSON column with literal per-field expected extraction values for all 30 samples (curated from the source documents, null for fields not stated). `run_pilot.py` parses it into `ground_truth["expected_fields"]` → threaded into the pipeline-result generation output. `scripts/sync_dataset.py` includes `expected_fields` in the dataset `expectedOutput`. `observability/scores.py` adds `expected_field_presence` (fraction of required expected fields extracted non-empty) as a deterministic ground-truth score attached by `run_pilot.py --scores`.
- Every trace now carries the full mandatory tag taxonomy documented in `AGENTS.md`: `mailroom` + run-context tag matching the trace environment (`pilot`/`live`/`misc`/`mock`) + `run-<n>` for re-runs + `source-<corpus>` for pilot/corpus runs (`graph/build_graph.py:_execute_run`; `scripts/run_pilot.py` passes the manifest `dataset`). The corpus is also recorded in trace metadata as `source`.
- Live LLM-as-a-Judge evaluation simplified from 3 evaluators + 11 observation rules to exactly **one cumulative evaluator** (`mailroom-pipeline-judge`, binary **CORRECT/MISS**) + **one rule** (`mailroom-pipeline-rule`): the pipeline now emits a single `pipeline-result` generation per document trace (`graph/build_graph.py:_emit_pipeline_result`, input = truncated document text, output = curated result), so each document costs exactly **one judge call**. The verdict is **grounded in the actual ground truth**: pilot runs pass `expected_doc_class`/`expected_stage` through `run_pipeline(ground_truth=...)` into the generation output, and the judge decides strictly against the expected class + document contents (binary CORRECT/MISS, enabling accuracy tracking); live runs without ground truth fall back to rubric judgment. `scripts/sync_evaluators.py` deploys the evaluator/rule and prunes the old per-agent evaluators and rules; the offline per-dimension judges (`agents/judge.py`, `scripts/run_quality_judges.py`) are unchanged for pilot deep audits, and `run_pilot.py --scores` continues to attach deterministic binary `class_correct`/`stage_correct` scores.
- Qwen model pricing verified against the live OpenRouter models API: `qwen/qwen3.7-flash` (`$0.03` / `$0.13` per 1M tokens) matches `config/taxonomy.yaml` `cost_models:` exactly, so per-token registry prices (3e-08 in / 1.3e-07 out) are confirmed accurate. The stale `qwen/qwen3.7-pro` registry entry (a phantom — no such OpenRouter model, never used by the pipeline) was deleted; the Langfuse model registry now contains exactly the `cost_models:` entries from `taxonomy.yaml`.
- Live LLM-as-a-Judge evaluators in Langfuse now run on `deepseek/deepseek-v4-flash` (via OpenRouter), matching the offline judge; evaluator versions were reset to a single clean v1 each (stale version history from repeated `--force` syncs removed).
- Langfuse project evaluator inventory cleaned: only the project-scope mailroom evaluators and their observation rules remain in use; the 22 Langfuse-managed template evaluators are platform-locked (`scope=managed`, API returns 403 on delete) and ignored.
- `contracts_specialist` `max_tokens` raised 4096 → 8192 in `config/taxonomy.yaml` (large contracts were truncating extraction JSON mid-run).
- `scripts/sync_dashboards.py` — idempotent sync of the mailroom health dashboards into Langfuse (definitions in version control): the **Mailroom Quality — per Prompt over Time** dashboard (average score, p95 latency, and total cost per prompt as LINE_TIME_SERIES widgets scoped to `environment any of [live, pilot]`, so a quality regression shows up as a trend automatically) and the **Production Health — Judges (Qwen & DeepSeek)** dashboard (LLM-as-a-judge throughput / P95 / P99 / errors). The quality widgets' environment filter was corrected from a dead `production` value (no mailroom run ever uses it) to the project's real environments.
- The two judge variant prompts are now first-class Langfuse-managed prompts (`mailroom-judge-classification`, `mailroom-judge-correctness`, registered in `llm/prompts.py:prompt_templates()` and synced): `agents/judge.py:judge_classification`/`judge_extraction_correctness` previously passed hardcoded `system_prompt=` overrides, skipping `system_prompt()`, so those generations were never linked to a prompt; they now fetch the managed variant and pass `langfuse_prompt=` like every other agent call.
- Ground-truth handling is now fail-closed: `run_pilot.py` and `sync_dataset.py` reject missing, malformed, or schema-incompatible `expected_fields` instead of silently downgrading a sample to class/stage-only scoring. All 30 manifest rows and all four Langfuse pilot datasets were validated and synced; the court-opinion subset was rerun with field-level scores.
- Prompt audit against Langfuse observation outcomes produced two targeted changes: `contracts_specialist` now explicitly prioritizes completing one compact schema-valid JSON object on long/truncated inputs, and `reporter` now treats nulls/redaction markers/placeholders as absent rather than facts. Prompts with clean current evidence, including sorter and court-opinion extraction, were left unchanged.
- Reporter completion starvation fixed: `reporter` now uses `reasoning_effort: none` and a 3072-token cap, and `agents/reporter.py` propagates that setting into its manually constructed OpenRouter request. A real smoke test produced a 2506-character report with zero reasoning tokens; the prior failure produced 2 visible tokens and 2048 reasoning tokens.
- All 13 managed prompts completed another evidence-driven iteration and were synced to Langfuse production: structured-output completion/grounding rules were added across the agent prompts, court-opinion and correspondence tie-break/absence rules were tightened, and the PDF transcriber contract was corrected to request markdown only. Triggered real runs covered the original specialist paths plus the judge paths; due-diligence and judge reasoning budgets were separately changed to `none`/2048 after observed truncation, yielding 2/2 due-diligence archived results and 2/2 clean judge dimensions on the follow-up run.

### Fixed

- Langfuse generation costs stayed null for every mailroom trace even after model registry entries were added: the Langfuse worker's Redis model-match cache holds a 24-hour "model not found" token per model string (`LANGFUSE_CACHE_MODEL_MATCH_TTL_SECONDS` defaults to 86400), and runs that used `qwen/qwen3.7-flash` before the registry entry existed had poisoned it. Re-syncing via `scripts/sync_models.py --force` (delete + create invokes Langfuse's `clearModelCacheForProject`) purged the stale token; cost inference now works and Langfuse backfilled `cost_details` for all historical generations. Verified: all 184 generations across the 12 pilot traces carry cost (~$0.063 total; price math confirmed token-exact). Note for future tooling: cost must be read from the observation `cost_details` field — `usage.input_cost`/`output_cost` are always null in API v2 responses.
- Run token budget enforced the wrong metric: `check_token_budget()` compared the `run_limits.max_total_output_tokens` cap against cumulative prompt+completion tokens, so large documents (e.g. a 52-page contract with ~28k input tokens per specialist call) tripped the 20k cap at the `compile-report` node boundary and aborted as failed. The cap now counts **completion tokens only**, matching its documented purpose (stuck/overly-verbose model guard); regression tests added in `tests/test_run_limits.py`.
- SQLite schema drift: `ensure_schema()` only created missing tables, so pre-existing databases lacked newer columns (e.g. `documents.scores`), causing `catalog_write_error`/`scores_persist_error` on every run. `storage/db.py` now diffs every model column against the live schema and adds missing ones via `ALTER TABLE ADD COLUMN` (idempotent, cached per DB URL).

## [0.2.2] - 2026-08-08

### Added

- Langfuse Prompt Management integration (`llm/prompts.py`): every agent's system prompt is now a managed prompt (`mailroom-<agent_name>`, `production` label) fetched at runtime and compiled with its variables, with the identical template shipped in code as a fallback when Langfuse is off or unreachable.
- `scripts/sync_prompts.py` — idempotent push of the local agent prompt templates into Langfuse prompt management (dry-run, force, and per-agent modes; new versions only when the template actually changed).
- `scripts/sync_langfuse_logs.py` — mirrors Langfuse traces (with nested observations, scores, and linked prompt versions) into `data/langfuse_logs/<run>/` plus an `index.json` for offline analysis.
- `scripts/run_quality_judges.py` — offline LLM-as-a-judge evaluation over a pilot run across three task-spec dimensions: classification correctness, extraction completeness, and extraction correctness; scores are attached to each sample's deterministic Langfuse trace and a calibration summary is appended to the pilot report.
- Judge agent extended with classification and correctness verdicts alongside completeness (`agents/judge.py`).
- Tests for the judge, prompt management, and score modules (`tests/test_judge.py`, `tests/test_prompts.py`, `tests/test_scores.py`).

### Changed

- All LLM agents now resolve their system prompts through `get_managed_prompt` and pass `langfuse_prompt=` on generation calls so every trace links to the exact prompt version used; `BaseAgent` supports system-prompt overrides and propagates the fetched prompt object.
- `observability/scores.py` extended with canonical score configs for the judge verdict dimensions (classification, completeness, correctness).
- README substantially expanded; `docs/agents.md`, `docs/architecture.md`, and `docs/configuration.md` updated; wiki pages (Agents, Architecture, Configuration) resynced.

### Fixed

- Operational Langfuse bugs: prompt fetch/compile/link behavior and score-config handling under the Langfuse backend.
- Trace-log mirroring quirks (e.g., the API rejecting most `order_by` formats — mirror now sorts locally).
- PDF transcription prompt handling for the managed-prompt flow.

### Removed

- `scripts/run_completeness_judge.py` (superseded by `scripts/run_quality_judges.py`).

## [0.2.1] - 2026-08-08

### Added

- Offline LLM-as-a-judge agent (`agents/judge.py`) that evaluates extraction completeness against the source document text; runs only via scripts, never inside the pipeline.
- Transient-failure retry for all LLM calls (`llm/retry.py`): `retry_chat_completion` with exponential backoff and jitter covering connection errors, timeouts, 429s, and 5xx only — client errors are never retried; every attempt is traced as its own generation.
- Quality scoring layer (`observability/scores.py`): canonical score configs auto-registered in Langfuse (idempotent), self-evident production scores emitted per document run (`parse_error`, `schema_valid`, `stage_completed`, classification/extraction confidence), ground-truth pilot scores (`class_correct`, `stage_correct`, `confidence_calibration_error`), and extraction-schema validation.
- `scripts/run_completeness_judge.py` — offline completeness audit over pilot runs (superseded in 0.2.2).
- Ground-truth score ingestion in `scripts/run_pilot.py` (`--scores` flag): attaches class/stage correctness and calibration scores to each sample's deterministic Langfuse trace; pilot report now includes extracted data per sample.
- Pipeline now emits and persists quality scores for every finished run (`graph/build_graph.py` + `storage/catalog.py:update_document_scores`).
- Config additions in `config/taxonomy.yaml`: per-agent `max_tokens` caps, `llm_retry` tunables, `pdf_direct_chars_per_page` threshold, and agent entries for `pdf_transcriber` and `judge`.
- Subagent definitions under `.opencode/agents/`: legal-changelog-auditor, mailroom-arch-optimizer, trace-log-analyst.
- Tests for base-agent behavior (`tests/test_agents/test_base.py`) and retry logic (`tests/test_llm_retry.py`).

### Changed

- All agent LLM calls now route through `retry_chat_completion` with per-agent `max_tokens` caps (default 4096).
- Structured-output boilerplate switched to the lowercase "json" wording that Qwen/Alibaba providers require verbatim — fixes JSON-mode 400 rejections.
- PDF transcription: clean text-based PDFs are extracted directly (no LLM pass); the LLM reformat pass only runs for scanned or garbled documents.
- Pilot mock mode keys canned responses off instruction content instead of JSON schema names.

### Fixed

- Async-safe persistence of pipeline quality scores into the document catalog.
- Pilot-run bugs: mock classification/confidence handling, score ingestion, and report structure.

## [0.2.0] - 2026-08-08

### Added

- Braintrust tracing backend as an alternative to Langfuse: new backend-agnostic observability facade (`observability/tracing.py`) selecting via `OBSERVABILITY_PROVIDER=auto|langfuse|braintrust|none`; OpenAI clients are auto-instrumented for whichever backend is active, so agent code never changes.
- SQLite-first storage: the default database is now a serverless SQLite file (`data/mailroom.db` — matters, documents, audit_log) auto-created on first use via `ensure_schema()`; Postgres remains available by setting `DATABASE_URL`. LangGraph checkpoints moved to SQLite (`langgraph-checkpoint-sqlite`) with a MemorySaver fallback.
- `pipeline/env.py` — a single `.env` loader shared by the watcher, API, ops monitor, LLM client, and scripts.
- Pilot assets: example legal PDFs (CUAD-derived, CC-BY-4.0) and source texts under `examples/`, a ground-truth `manifest.csv`, `ATTRIBUTION.md`, `scripts/prepare_samples.py` (including HuggingFace dataset download support), and `scripts/run_pilot.py` (mock and real-LLM pilot runs with baseline diffs).
- Langfuse skill bundle under `.opencode/skills/langfuse/`, `AGENTS.md`, and per-package READMEs.
- New runtime dependencies: `langgraph-checkpoint-sqlite`, `aiosqlite`, `pypdf`, `pdfplumber`, `reportlab`, `braintrust`.
- Tests for observability, PDF transcription, and sample manifest integrity.

### Changed

- Default LLM models switched from `openai/gpt-4o` to `qwen/qwen3.7-flash` for the sorter, specialists, and reporter, and to `deepseek/deepseek-v4-pro` for the boss (`config/taxonomy.yaml`).
- Langfuse setup expanded to structured per-document tracing: one deterministic trace per document (seeded by filename), `session_id=matter_id` grouping, and verb-first traced node spans.
- Deployment and architecture docs updated for SQLite-first storage and the Braintrust option; wiki pages resynced.

## [0.1.0] - 2026-08-08

### Added

- Initial release of the multi-agent legal document processing pipeline.
- Core pipeline: an 11-node LangGraph state machine (ingest, classify, extract, report, catalog, archive, with retry loops, BossAgent conflict adjudication, and human-review routing) with conditional routing in `graph/routing.py` and checkpointed crash recovery.
- Specialist LLM agents: `SorterAgent` classifier, five extraction specialists (contracts, corporate records, due diligence, correspondence, compliance), `BossAgent`, `ReporterAgent`, and the procedural `Archivist`.
- Image and PDF text extraction: vision-capable `ImageExtractor` and `PDFTranscriber` agents with a `pdftotext` CLI fallback, wired into the ingest stage with per-extension file handling.
- Provider-agnostic LLM layer (`llm/client.py`, `llm/providers.py`) over OpenRouter, plus a local-model cutover workflow (`cutover.py` with `--list`, `--recommend`, `--validate`).
- Filesystem bin pipeline (`inbox → processing → archive | review | failed`) via `pipeline/bins.py`; watchdog-based filesystem watcher, FastAPI service with upload/status/audit endpoints, and scheduled ops monitor.
- SQLAlchemy storage layer: document catalog, hash-chained audit log, and matter records; Docker Compose setup for Postgres, ClickHouse, Langfuse, and Ollama.
- `config/taxonomy.yaml` as the single source of truth for document classes, confidence thresholds, file extensions, and the agent-to-model mapping.
- Langfuse tracing setup for per-document traces.
- Full documentation set: README, `docs/` (agents, api, architecture, configuration, deployment, local-models, testing), wiki pages, and `wiki/sync-wiki.sh`.
- Test suite with mocked LLM: agent unit tests, routing tests, audit-log tests, and end-to-end pipeline tests, plus text fixtures for all document classes.

### Changed

- Pipeline ingest refactored for robust file-type handling with per-extension extraction (text, image, PDF, DOCX); specialist dispatch driven by the taxonomy doc-class config.
- Document catalog records are upserted on write; archive and catalog writes made async-safe.
- File moves in `pipeline/bins.py` now use `shutil.move`; default file-extension fallback added.
- Watcher and ops-monitor robustness fixes; audit log writer made async-safe; `Matter.opened_at` made timezone-aware.

[Unreleased]: https://github.com/Exios66/llm-mailroom/compare/v0.5.0...HEAD
[v0.5.0]: https://github.com/Exios66/llm-mailroom/compare/v0.4.1...v0.5.0
[v0.4.1]: https://github.com/Exios66/llm-mailroom/compare/v0.4.0...v0.4.1
[v0.4.0]: https://github.com/Exios66/llm-mailroom/compare/v0.3.2...v0.4.0
[v0.3.2]: https://github.com/Exios66/llm-mailroom/compare/v0.3.1...v0.3.2
[v0.3.1]: https://github.com/Exios66/llm-mailroom/compare/v0.3.0...v0.3.1
[v0.3.0]: https://github.com/Exios66/llm-mailroom/compare/v0.2.2...v0.3.0
[v0.2.2]: https://github.com/Exios66/llm-mailroom/compare/v0.2.1...v0.2.2
[v0.2.1]: https://github.com/Exios66/llm-mailroom/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/Exios66/llm-mailroom/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/Exios66/llm-mailroom/releases/tag/v0.1.0
