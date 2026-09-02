# Sister Repositories & the Mailroom Umbrella

llm-mailroom does not fly alone. It is the pipeline at the center of a small
constellation of governed repositories — each with its own repo, board
discipline, and release train — plus derived artifacts hosted elsewhere. Since
2026-08-30 the whole constellation also lives as **one monorepo**
([`mailroom-dev`](https://github.com/Exios66/mailroom-dev)): every family
repo is a git-subtree package under `packages/`, wired as a single `uv`
workspace, with the monorepo as the **source of truth for active
development**. This page maps who's who, what flows between them, and where
the canonical state of each lives. (Links verified 2026-08-23; monorepo
verified 2026-08-30.)

```
                        ┌──────────────────────────────┐
                        │          mailroom-dev        │
                        │  THE MONOREPO — uv workspace │
                        │  git-subtree packages + hub  │
                        │  task board (TASKS.md)       │
                        └──────────┬───────────────────┘
            sync_packages.py      │ subtree pull / push
            (status/pull/push)    ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│  llm-entity-extraction │   │        llm-mailroom          │
│  prompt-experiment loop│   │  (this repo — the pipeline)  │
└──────────┬─────────────┘   └──────────┬───────────────────┘
           │            ┌───────────────┴───────────────┐
           ▼            ▼                               ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│  llm-dojo-scoring      │◀──│        The-Mailroom          │
│  scoring engine        │   │   pixel-art visual engine    │
└────────────────────────┘   └──────────┬───────────────────┘
                 │                      ▼
                 │        (reads this repo's Langfuse project — US cloud:
                 │         every envelope, badge, verdict, metric on screen)
┌────────────────────────┐
│ agent-mailroom |       │  sibling stand-alone mailrooms / sandboxes live
│ local-mailroom-sandbox │  in the monorepo too (same law, independent train)
└────────────────────────┘

corpus feeds:   Enron-Evaluation-Environment, claims-data-eda   (virtual members)
derived site:   llm-mailroom-graph (graphify knowledge-graph site)
HF datasets:    Lucius-Morningstar/* (published eval/corpus surfaces)
```

## At a glance

| Repository | Role | Relationship to mailroom |
|---|---|---|
| [mailroom-dev](https://github.com/Exios66/mailroom-dev) | **Monorepo / hub** — every constellation repo as a git-subtree `packages/` member in ONE uv workspace; hub task board `governance/TASKS.md`; sub-package sync driver `scripts/sync_packages.py` | **Development source of truth for cross-repo work** — this repo is `packages/llm-mailroom` there; sync via subtree `pull`/`push` (issue #2, HUB-001/002) |
| [llm-entity-extraction](https://github.com/Exios66/llm-entity-extraction) | Prompt-experiment loop: prompt versions × models over CUAD/LegalBench/MAUD corpora | **Sister repo.** Source of the vendored LangChain sorter/contracts prompts; shares ONE kanban board with this repo |
| [llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring) | Deterministic, field-type-aware scoring engine (metric registry, dedicated specialist suites, sorter subclass catalogs, computable intake clerk, prompt catalog, `local_vs_api` serving comparison) | **Upstream governed dependency**, pinned in `pyproject.toml` (`@v0.12.2` / [PR #11](https://github.com/Exios66/llm-dojo-scoring/pull/11)); auto-bump via `.github/workflows/bump-dojo-scoring.yml` |
| [Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment) | EDA + pipeline-ready correspondence dataset from the CMU Enron corpus | **Corpus feed** for the `correspondence` doc class; publishes HF datasets consumed by eval loops. Virtual monorepo member (no build) |
| [claims-data-eda](https://github.com/Exios66/claims-data-eda) | Insurance-claims candidate-corpus EDA (CMS DE-SynPUF direction) | **Corpus feed (candidate)** for the `insurance_claim` doc class — its honest-gap benchmark source. Virtual monorepo member (no build) |
| [atticus-investigation](https://github.com/Exios66/atticus-investigation) | LegalBench classification prompt-engineering pipeline | **Eval sibling**: same prompt-version × model methodology, LegalBench focus |
| [The-Mailroom](https://github.com/Exios66/The-Mailroom) | Pixel-art visual engine + hosted Observatory Space — floor, review siding, Inbox enqueue, inspector, sessions, metrics — plus a TUI console | **Downstream visualizer** — Langfuse-only display; Inbox / REVIEW proxy this API via `MAILROOM_PIPELINE_URL` + token + `/v1` ([PR #30](https://github.com/Exios66/The-Mailroom/pull/30)) |
| [agent-mailroom](https://github.com/Exios66/agent-mailroom) | Self-contained mailroom: one state machine per document, specialist agents at desks (Electron/TUI/live floor) | **Sibling implementation** — same doctrine, independent train; monorepo member (`packages/agent-mailroom`) |
| [local-mailroom-sandbox](https://github.com/Exios66/local-mailroom-sandbox) | Local-first experiment sandbox (Ollama, vLLM, llama.cpp) | **Sibling sandbox** — local-model experiments; monorepo member (`packages/local-mailroom-sandbox`) |
| [llm-mailroom-graph](https://exios66.github.io/llm-mailroom-graph/) | Interactive graphify knowledge graph of this codebase | **Derived site** — build artifact only, never committed here |
| [llm-entity-extraction-graph](https://exios66.github.io/llm-entity-extraction-graph/) | Interactive graphify knowledge graph of the sister experiment loop | **Derived site** — companion map of the sister repo's code structure |

## mailroom-dev — the monorepo (since 2026-08-30)

The Mailroom Umbrella's single checkout: `git clone Exios66/mailroom-dev`
reproduces **every** family repo under `packages/` (git subtrees, own
history), and one `uv sync` installs the whole workspace editable against a
single `uv.lock` — no cross-repo pip installs, no import-path juggling.

- **Layout**: `packages/<name>/` per standalone repo — this repo is
  `packages/llm-mailroom`. `Enron-Evaluation-Environment`, `claims-data-eda`
  and `llm-mailroom-graph` are **virtual members** (`package = false`): their
  data/code stays colocated but nothing installs from them.
- **Workspace rules** (monorepo `AGENTS.md`): member `pyproject.toml` files
  **keep their published git pins** — dev redirection happens ONLY through
  `[tool.uv.sources]` tables (this repo's table currently redirects
  `llm-dojo-scoring` to the workspace). `pip install .` inside the package /
  subtree keeps using the pins, so Docker/Railway/HF Spaces builds are
  unaffected.
- **Sub-package sync**: `python scripts/sync_packages.py {status|pull|push|snapshot}`
  reconciles the subtree mirrors with `Exios66/*` (baseline cursor
  `scripts/packages_sync.json`; `status` recomputes live drift). The monorepo
  is the **dev source of truth**; standalone-repo fixes that already landed
  here win merges unless the upstream supersedes them.
- **Cross-repo task board**: `governance/TASKS.md` (hub scope: workspace
  wiring, cross-package governance, sync, docs, releases). Read it before any
  hub-scope work; package-scoped work keeps each package's own board (e.g.
  the entity repo's `MESSAGE_BOARD.md`).
- **Test gates**: suites run one package per pytest invocation (`uv run pytest
  packages/llm-mailroom/src/tests`, …) — several packages ship colliding
  top-level `tests` packages.
- **Heavy assets pruned**: example PDFs, demo screenshots, report archives are
  NOT in the monorepo (see `docs/examples/samples/` note in
  [Agents](agents.md)); monorepo tests skip when a pruned fixture is absent.

Every package still carries its own `AGENTS.md`, docs, tests and deploy
configs — this page (and this repo's `docs/`) remain canonical for the
pipeline; the monorepo root `README.md` is the hub manual.

## llm-entity-extraction — the sister loop

The experiment environment where prompts are born and measured before they
reach this pipeline:

- Measures how well prompt versions classify legal documents and extract
  entities — every run produces one append-only record in its
  `reports/experiment_log.jsonl`.
- Its champion prompts are vendored into this repo under
  `langchain_agents/` (`sorter`, `contracts_specialist`) with the full
  version lineage (`PROMPT_VERSIONS`) carried along so evaluation can pin
  exact versions.
- **Governance:** one shared kanban board (`board/MESSAGE_BOARD.md` +
  discussion log in that repo) tracks cards for BOTH repos. Cross-repo work
  = one card, one issue, both changelogs. Never create a mailroom-side board.
- Release discipline there: semver tags trace single commits; this repo's
  vendored copies are refreshed deliberately against its `main`.

## llm-dojo-scoring — the upstream scoring engine

The scoring layer both mailroom and entity-extraction consume:

- Field-type-aware deterministic scoring (id/date/money normalization,
  Jaro-Winkler/token-set names, SQuAD-style token F1, Hungarian bipartite
  list matching, optional embedding rescue) — never exact-match-on-everything.
- Metric registry with T0–T3 tiers, task-aware dispatch (`score_task`),
  **agent profiles** covering every mailroom agent, and
  `DOC_TYPE_BUNDLES` keyed on the processed document classes with the
  explicit-fallback honesty resolver (`resolve_doc_bundle()`).
- Pinned as a git dependency (`@v0.12.2` at time of writing); mailroom wires
  its `taxonomy.yaml` scoring block onto package Settings via
  `observability/field_scoring.py` (a deprecation shim — imports should move
  to `llm_dojo_scoring.field_scoring`).
- **v0.12.2** ([PR #11](https://github.com/Exios66/llm-dojo-scoring/pull/11))
  is additive core-dependency alignment: `jellyfish>=1.0` moves into the
  base install (no silent `difflib` fallback), plus new optional extras
  `tracing` / `all` (and `openai` on the `embeddings` extra). Scoring
  formulas from v0.12.1 are unchanged.
- **v0.12.1** ([PR #10](https://github.com/Exios66/llm-dojo-scoring/pull/10))
  adds the `local_vs_api` serving suite: `compare_serving` tables, scorecards,
  and cost cards for local-vs-API-key runs. Document-pipeline scoring formulas
  from v0.11.0 are unchanged.
- **v0.11.0** ([PR #8](https://github.com/Exios66/llm-dojo-scoring/pull/8))
  is additive on the 0.10.0 scoring surface: T0/T1 `MetricDef`s carry
  `citation` / `inclusion` / `ground_truth`; `llm_dojo_scoring.prompts`
  vendors production + docclass templates with metric names in catalog
  metadata only (anti-priming). `field_presence` stays unemitted.

### Auto-bump when dojo publishes

Mailroom keeps the dojo pin current via
`.github/workflows/bump-dojo-scoring.yml` and
`src/scripts/bump_dojo_scoring.py`:

1. **Preferred:** from `llm-dojo-scoring`'s release workflow, fire a
   `repository_dispatch` at this repo:

   ```yaml
   - name: Notify llm-mailroom to bump pin
     uses: peter-evans/repository-dispatch@v3
     with:
       token: ${{ secrets.MAILROOM_DISPATCH_TOKEN }}  # PAT with repo scope on llm-mailroom
       repository: Exios66/llm-mailroom
       event-type: dojo-scoring-released
       client-payload: '{"tag": "v0.12.2"}'  # use github.event.release.tag_name
   ```

2. **Backstop:** the workflow also runs on a daily cron and via
   `workflow_dispatch` (optional tag / dry-run inputs), polling
   `Exios66/llm-dojo-scoring` releases/latest.

3. When the pin is behind, the workflow opens (or updates) a PR on
   `cursor/bump-dojo-scoring-<tag>` that rewrites `pyproject.toml` plus the
   documented pin references. Merge after CI is green.

> In the **monorepo**, the nested `.github/workflows/` is not read by GitHub
> (only the hub root's workflows are) — the auto-bump is **release-time only**
> there: run `src/scripts/bump_dojo_scoring.py --check/--apply` manually when
> cutting a mailroom release, then `sync_packages.py push` the pin change.

Local check / apply:

```bash
PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --check
PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.12.2
```

## Corpus feeds

- **[Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment)**:
  builds the correspondence-class corpus (517K-message index → stratified
  pipeline dumps), with the shared heuristic labeler as ground truth.
  Published datasets live under the
  [`Lucius-Morningstar`](https://huggingface.co/Lucius-Morningstar) HF org
  (`enron-correspondence`, `enron-correspondence-dedup`, …).
- **[claims-data-eda](https://github.com/Exios66/claims-data-eda)**:
  exploratory analysis toward an insurance-claims benchmark. Hub rows today
  are CMS DE-SynPUF source tables; dojo 0.10.0+ ships
  **`determination_consistency` / `amount_exactness`** as real scorers. CMS GT
  is homogeneous (all-approved / empty denials), so Hub
  `determination_consistency` is gated as a quality KPI. Mailroom exercises
  the scorer on a local approved/denied/partial contrast pack (documented in
  [Agents](agents.md)).
- **Honesty (dojo 0.11.0 suites):** `compliance_filing` has zero
  `mailroom-corpus` rows (HF `--real` omits it; local fixture pack is
  mock/check only). `corporate_record` has 39 Hub subclass rows and **no
  external extraction benchmark**; schema-complete extraction GT is the
  local pack. `court_opinion` / `due_diligence` are retired from live
  mailroom (`retired=True`).
- **[atticus-investigation](https://github.com/Exios66/atticus-investigation)**:
  LegalBench classification sibling — its methodology (prompt versions ×
  models, paired-bootstrap ablations) is the same doctrine this constellation
  follows.

## The-Mailroom — the visual engine

- **[The-Mailroom](https://github.com/Exios66/The-Mailroom)** (v0.3+) renders
  every pipeline run as an animated conveyor of document envelopes — sorter,
  specialist bays, judge gate, boss's desk, reporter, archive — grouped into
  three rooms, plus a human-review siding queue, per-trace inspector,
  matter/session explorer, and live metrics. Surfaces: `mailroom-web`
  (FastAPI + vanilla JS on :8001) and `mailroom-tui` (AgentLab-style live
  console).
- **Langfuse is its sole source of truth**: every envelope, badge, verdict,
  and metric is derived from this repo's Langfuse project — nothing is
  fabricated, nothing falls back to canned data. Demo mode seeds synthetic
  runs INTO Langfuse (env `demo`) rather than bypassing it.
- **Schema mirror duty (its #1 maintenance rule)**: when THIS repo changes
  span names, node order, agent roster, doc classes, confidence thresholds,
  or judge score names, The-Mailroom must update `mailroom_ui/pipeline_schema.py`
  and `mailroom_ui/trace_interpreter.py` in the same change window — new spans
  render as `unknown` stage until mirrored. `MAILROOM_TAXONOMY` can point it
  at this repo's `src/config/taxonomy.yaml` live instead of its bundled mirror.
  Current intake contract: span `normalize-intake` (INGEST), agent `intake`,
  HF runner `src/scripts/run_hf_pilot.py` (session `pilot-hf-<stamp>`, tag
  `source-mailroom-corpus` on the v5 full corpus — other Hub sets use their
  own `source-*` tag from `pipeline/hf_corpora.py`, ground truth on trace
  input/metadata including `expected_hf_class`). Production session
  `pilot-hf-20260825T044207Z` is the reference five-doc Qwen 3.7-Flash subset.
- **Reconsideration (The-Mailroom [PR #14](https://github.com/Exios66/The-Mailroom/pull/14)):**
  the visualizer parks archived objective misses as **RECONSIDER** from
  ground truth / judge / scores — never from self-reported confidence.
  This pipeline mirrors those cause tokens in `pipeline/reconsideration.py`
  and acts on them **before** catalog write: GT class miss → Lane A even at
  0.99; reviewer still-wrong → human review; hollow extract or expected-field
  coverage below `confidence.low` → retry then review; failed `compile_report`
  withholds `catalog_write`.
- **First-pass / STP metrics (production, no ground truth):** every finished
  run now emits the registered dojo score `success_rate` (0/1). It is 1 only
  when the document archived in one hop — no classify/extract retry, Lane A,
  arbiter, boss, human review, guardrail, parse/schema failure, or transient
  provider self-loop. Incoming live documents are zero-shot; this flag does
  **not** consult `class_correct`, field GT, or the hosted LLM-judge overlay
  (those stay eval-only). The-Mailroom metrics (pixel METRICS tab, hosted
  Observatory, TUI `m`) tile **FIRST PASS** (count) and **FIRST-PASS RATE**
  from that score, with a routing-path/`retried` fallback for traces emitted
  before the producer score existed. Do not flatten FIRST PASS into ARCHIVED
  (archived includes retries that later succeeded) or RECONSIDER (GT/judge
  overlay). Langfuse **Mailroom Performance** dashboard charts the live+pilot
  rate and count. `GET /ops/status` reports `first_pass` / `first_pass_rate`
  from the catalog.
- **Live floor (The-Mailroom [PR #16](https://github.com/Exios66/The-Mailroom/pull/16)):**
  the visualizer re-enriches in-flight traces every poll and reads producer
  liveness from `GET /health` (`checks.watcher`, `inbox_pending`). This
  pipeline flushes after each graph node (`output.stage` on the span),
  embeds the inbox watcher in the API lifespan by default, and treats
  `on_moved` / `on_modified` inbox events so an upload appears on the floor
  within one poll tick. `MAILROOM_PIPELINE_URL` on the visualizer should
  point at this API (`http://127.0.0.1:8000` on a shared host, or the
  public producer Space URL when the Observatory is itself a Space).
- **Inbox enqueue + Observatory cards (The-Mailroom [PR #30](https://github.com/Exios66/The-Mailroom/pull/30)):**
  Observatory **Queue a document** proxies multipart files to
  `POST /v1/upload` (202). Unconfigured visualizer returns 503 — no
  fabricated catalog row. Cards show classification hit/miss/pending and a
  headline strip; Langfuse snapshot cache (`MAILROOM_TRACE_CACHE_DIR`) is
  visualizer-side. Pairing knobs:
  `MAILROOM_PIPELINE_URL` + `MAILROOM_PIPELINE_TOKEN` +
  `MAILROOM_PIPELINE_API_PREFIX=/v1`. The-Mailroom `publish_space.py`
  copies those as Observatory Space secrets/variables. Checklist:
  [`deploy/space/PAIRING.md`](../deploy/space/PAIRING.md).
- **REVIEW resolve (The-Mailroom [PR #18](https://github.com/Exios66/The-Mailroom/pull/18)
  + [PR #20](https://github.com/Exios66/The-Mailroom/pull/20)):**
  the visualizer proxies operator decisions to this API — never holds producer
  keys in the browser. REVIEW desk buttons (Approve / Reject / Requeue, class /
  subtype selects, Open original / text pane) call the producer so operators
  never type endpoints. Producer surface: `GET /lookup`, `GET /audit/{doc_id}`,
  `POST /review/{doc_id}/resolve` with `disposition=resume|record|requeue`
  (plus mailroom-local `complete`), optional `doc_type` / `doc_subclass`
  (written to the parked manifest on resume; stamped on the inbox sidecar on
  requeue), and `GET /documents/{doc_id}/source` (`?download=1` for original
  bytes). Set `MAILROOM_PIPELINE_URL` + `MAILROOM_PIPELINE_TOKEN` on the
  visualizer (not `MAILROOM_API_URL`, which is TUI → visualizer `:8001`). The
  producer must be **reachable from the visualizer process** — localhost
  `:8000` for a laptop pair, or the published Hugging Face Docker Space
  (`src/scripts/publish_space.py`, URL `https://lucius-morningstar-mailroom-producer.hf.space`)
  for the hosted Observatory. Local compose:
  `docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build`.
  `GET /health` advertises `producer` / `review_resolve`. Full audit parse:
  `GET /audit` / `scripts/analyze_audit_db.py`.
- **Governance:** fully governed member of the family — own `AGENTS.md`, own
  semver release train (v0.2.0), own test suite (never hits real Langfuse),
  own wiki. It is a downstream OBSERVER: dependency of no family repo — the
  coupling is the shared trace contract.

## Derived artifacts

- **[llm-mailroom-graph](https://exios66.github.io/llm-mailroom-graph/)** —
  interactive AST-derived knowledge graph of this codebase (built with
  [graphify](https://github.com/Graphify-Labs/graphify); the vendored agent
  skill lives in `.opencode/skills/graphify/`). Graphs are build artifacts:
  query them for structure questions ("what calls X", "where does Y route"),
  but board rules and source win any disagreement.
- **[llm-entity-extraction-graph](https://exios66.github.io/llm-entity-extraction-graph/)**
  — companion graphify map of the sister experiment loop's codebase.
- **Hugging Face — [`Lucius-Morningstar`](https://huggingface.co/Lucius-Morningstar)** —
   the family's published dataset surface. **`mailroom-corpus` schema v7**
  (1,650 docs, Hub SHA `fc1f211c…`, v7 = issue #5 correspondence intent
  hydration: canonical 8-class intents + `intent_source` / `intent_confidence`
  / `intent_status` provenance on the ground_truth config) is the targeted
  full pipeline corpus (CUAD contracts, MAUD merger agreements, S-1
  corporate records, Enron correspondence sample, CMS insurance claims).
  **`docclass-pilot`** is the
  class × subclass example pack (48 strata — every type and subtype in that
  parent). Other pipeline-ready Hub sets (`enron-correspondence-dedup`
  ~247k, `cms-desynpuf-insurance-claims`, `mailroom-cuad-contracts[-full]`)
  ingest the same way via `run_hf_pilot.py --dataset`. `legalbench-full` is
  a LegalBench CLI task pack, not a document-pipeline ingest. One split rule
  for the whole family (`md5(filename) % 10 == 0 → test`), owned by
  entity-extraction's publisher scripts. Local committed PDFs under
  `docs/examples/samples/` remain PDF-ingest fixtures — they are not the
  class catalog.

## Governance notes

- Every repo above runs the same working agreement: read the board first,
  no card = no work, append-only prompt versioning, changelog entry in the
  ship commit, evidence before "done".
- **Hub-scope work is tracked on the monorepo board**
  (`mailroom-dev/governance/TASKS.md`, cards `HUB-00N`) — cross-package
  wiring, sub-package sync and docs alignments are claimed there before
  editing, and committed with `HUB-00N:` references. Standalone-package work
  keeps each package's own board (`read the board first` applies on both
  sides).
- Cross-repo changes ride **one card + one issue** with both repos'
  CHANGELOGs updated in the same pass. Docs that describe the constellation
  (this page, wiki pages, README umbrella sections) are maintained in the
  standalone repo and arrive in the monorepo via subtree `pull` — and vice
  versa on `push`.
- Dependency pins are audited after every upstream release (pin ↔ tag ↔
  import-time validation must agree across `pyproject.toml`,
  `requirements*.txt`, and installed provenance). In the monorepo the pins
  keep their published meaning; `[tool.uv.sources]` redirects are dev-only.
