# Sister Repositories & the Mailroom Umbrella

llm-mailroom does not fly alone. It is the pipeline at the center of a small
constellation of governed repositories — each with its own repo, board
discipline, and release train — plus derived artifacts hosted elsewhere. This
page maps who's who, what flows between them, and where the canonical state of
each lives. (All links verified 2026-08-23.)

```
                        ┌──────────────────────────────┐
                        │   llm-entity-extraction      │
                        │   prompt-experiment loop     │
                        │   (sister repo, shared board)│
                        └──────────┬───────────────────┘
              champion prompts     │    shared kanban board
              (version keys)       ▼    governs BOTH repos
┌────────────────────────┐   ┌──────────────────────────────┐
│  corpus feed repos     │──▶│        llm-mailroom          │
│  Enron-Eval-Environment│   │  (this repo — the pipeline)  │
│  claims-data-eda       │   └──────────┬───────────────────┘
│  atticus-investigation │              │ pinned dependency @c3dbe9da (0.9.0)
└────────────────────────┘              ▼
                          ┌──────────────────────────────┐
                          │     llm-dojo-scoring         │
                          │  upstream scoring engine     │
                          └──────────────────────────────┘

                          ┌──────────────────────────────┐
                          │        The-Mailroom          │
                          │   pixel-art visual engine    │
                          └──────────────────────────────┘
             reads this repo's Langfuse project (US cloud) — every envelope,
             badge, verdict, and metric on screen is trace-derived

derived artifact: llm-mailroom-graph (graphify knowledge-graph site)
HF datasets:      Lucius-Morningstar/* (published eval/corpus surfaces)
```

## At a glance

| Repository | Role | Relationship to mailroom |
|---|---|---|
| [llm-entity-extraction](https://github.com/Exios66/llm-entity-extraction) | Prompt-experiment loop: prompt versions × models over CUAD/LegalBench/MAUD corpora | **Sister repo.** Source of the vendored LangChain sorter/contracts prompts; shares ONE kanban board and discussion log with this repo |
| [llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring) | Deterministic, field-type-aware scoring engine (metric registry, dedicated specialist suites, sorter subclass catalogs) | **Upstream governed dependency**, pinned in `pyproject.toml` (`@c3dbe9da`, package 0.9.0 / [PR #4](https://github.com/Exios66/llm-dojo-scoring/pull/4)); consumed through thin re-export shims |
| [Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment) | EDA + pipeline-ready correspondence dataset from the CMU Enron corpus | **Corpus feed** for the `correspondence` doc class; publishes HF datasets consumed by eval loops |
| [claims-data-eda](https://github.com/Exios66/claims-data-eda) | Insurance-claims candidate-corpus EDA (CMS DE-SynPUF direction) | **Corpus feed (candidate)** for the `insurance_claim` doc class — its honest-gap benchmark source |
| [atticus-investigation](https://github.com/Exios66/atticus-investigation) | LegalBench classification prompt-engineering pipeline | **Eval sibling**: same prompt-version × model methodology, LegalBench focus |
| [The-Mailroom](https://github.com/Exios66/The-Mailroom) | Pixel-art visual engine rendering every pipeline run as an animated document conveyor — floor, review siding, inspector, sessions, metrics — plus a TUI console | **Downstream visualizer** — driven SOLELY by this repo's Langfuse project (nothing fabricated, no local fallback); mirrors the pipeline's trace contract in its schema layer |
| [llm-mailroom-graph](https://exios66.github.io/llm-mailroom-graph/) | Interactive graphify knowledge graph of this codebase | **Derived site** — build artifact only, never committed here |
| [llm-entity-extraction-graph](https://exios66.github.io/llm-entity-extraction-graph/) | Interactive graphify knowledge graph of the sister experiment loop | **Derived site** — companion map of the sister repo's code structure |

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
- Pinned as a git dependency (`@c3dbe9da` / package 0.9.0 at time of writing); mailroom wires
  its `taxonomy.yaml` scoring block onto package Settings via
  `observability/field_scoring.py` (a deprecation shim — imports should move
  to `llm_dojo_scoring.field_scoring`).

## Corpus feeds

- **[Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment)**:
  builds the correspondence-class corpus (517K-message index → stratified
  pipeline dumps), with the shared heuristic labeler as ground truth.
  Published datasets live under the
  [`Lucius-Morningstar`](https://huggingface.co/Lucius-Morningstar) HF org
  (`enron-correspondence`, `enron-correspondence-dedup`, …).
- **[claims-data-eda](https://github.com/Exios66/claims-data-eda)**:
  exploratory analysis toward an insurance-claims benchmark — the future
  ground truth for the `insurance_claim` specialist (today synthetic-only by
  design; honest gap documented in [Agents](Agents)).
- **[atticus-investigation](https://github.com/Exios66/atticus-investigation)**:
  LegalBench classification sibling — its methodology (prompt versions ×
  models, paired-bootstrap ablations) is the same doctrine this constellation
  follows.

## The-Mailroom — the visual engine

- **[The-Mailroom](https://github.com/Exios66/The-Mailroom)** (v0.2.0) renders
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
  `source-docclass-merged`, ground truth on trace input/metadata including
  `expected_hf_class`). Production session `pilot-hf-20260825T044207Z` is the
  reference five-doc Qwen 3.7-Flash subset.
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
  the family's published dataset surface (CUAD contracts mirrors, LegalBench
  packs, docclass-merged, enron-correspondence/-dedup). One split rule for
  the whole family (`md5(filename) % 10 == 0 → test`), owned by
  entity-extraction's publisher scripts.

## Governance notes

- Every repo above runs the same working agreement: read the board first,
  no card = no work, append-only prompt versioning, changelog entry in the
  ship commit, evidence before "done".
- Cross-repo changes ride **one card + one issue** with both repos'
  CHANGELOGs updated in the same pass.
- Dependency pins are audited after every upstream release (pin ↔ tag ↔
  import-time validation must agree across `pyproject.toml`,
  `requirements*.txt`, and installed provenance).
