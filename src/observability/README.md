# observability/

Tracing, scoring, and evaluation plumbing for the mailroom pipeline.

## Where things live (post KANBAN-061)

The field-scoring implementation is **owned by the shared package**
[`llm-dojo-scoring`](https://github.com/Exios66/llm-dojo-scoring) (v0.11.0).
This repo keeps only a backward-compatibility shim.

| Module | Status |
| --- | --- |
| `tracing.py`, `langfuse_setup.py`, `phoenix_setup.py` | local — tracing facade/backends |
| `scores.py` | local — Langfuse score configs; names validated against the dojo metric registry at import |
| `field_scoring.py` | **deprecated shim** over `llm_dojo_scoring.field_scoring` |

## `field_scoring.py` shim

Importing it emits a `DeprecationWarning` and re-exports everything from
`llm_dojo_scoring.field_scoring`. Mailroom-specific behavior that stayed local:

- `get_type_bands()` / `field_is_ambiguous()` / `warm_embedding_model()` —
  taxonomy-driven glue (`field_scoring.type_bands` in `config/taxonomy.yaml`)
- `get_field_types()` auto-loads `config/taxonomy.yaml`; the package version
  requires an explicit taxonomy dict
- taxonomy wiring runs at import via the package's `configure(**overrides)`
  (values set verbatim; YAML lists are coerced to the tuple/set forms the
  package stores)

New code should import from `llm_dojo_scoring.field_scoring` directly.
Tests that patch internals (`_get_embedding`) must patch
`llm_dojo_scoring.field_scoring`, not this shim.

## Score schema governance

`scores.py::SCORE_CONFIGS` is checked against `load_registry().metrics` at
import time. Adding a score name here without registering it upstream fails
fast with a `RuntimeError` naming the drifted entries — register new metrics
in llm-dojo-scoring first, then use them here.

**Class KPIs after #38/#39:** exact class match is the only classification
score. `merger_agreement` (MAUD) is not `contract` (CUAD). Dojo 0.11.0's
`llm_dojo_scoring.mailroom.align_doc_type` still aliases them — mailroom
does not call it. Grounded runs emit `class_correct` from
`emit_pipeline_scores` via `observability.classification_scoring`. HF reports
keep `aligned_accuracy` as a deprecated JSON alias of exact
(`aligned_equals_exact: true`) so older readers do not break; markdown no
longer labels it merger≡contract. Subclass accuracy is scored against the
v5 Hub class × subtype strata.

`get_suite("intake")` (dojo PR #5) scores the pre-sorter clerk against gold
(`intake_prep_completeness`, changed/messy rates, hyphen/blank counts). That
path returns a dict, not an `ExtractionScoreResult` — see
`suite_scoring.score_and_log_intake`.

## Honesty gaps (dojo 0.11.0)

`observability/honest_gaps.py` reads `honest_gap` / `in_corpus` / `retired`
from `get_suite(doc_class)` and attaches a slim block as **trace metadata**
(never tags — tags are immutable/upfront). Registered extras
(`determination_consistency`, field-micro F1/F2) are SCORE_CONFIGS names
that exist in the v0.11.0 registry. v0.11.0 adds `citation` / `inclusion` /
`ground_truth` on T0/T1 `MetricDef`s and an importable prompt catalog
(`llm_dojo_scoring.prompts`). `field_presence` is documented as unemitted —
do not treat a missing key as 0.0.

`observability/local_eval_packs.py` closes the operational holes Hub cannot
(mock/check only; never billed as `--real` Hub accuracy):

| Class | Gap | What mailroom does |
| --- | --- | --- |
| `insurance_claim` | CMS GT homogeneity (all-approved) | Gate Hub `determination_consistency` as a quality KPI; local contrast pack (approved/denied/partial) exercises the scorer |
| `compliance_filing` | zero Hub rows; HF `--real` excludes the class | Local fixture pack (10-K + state filing) scored on `--check` / `--mock` |
| `corporate_record` | 39 Hub subclass rows; no external extraction benchmark | Local schema-complete extraction pack; join extra Hub GT columns when present |
| `court_opinion` / `due_diligence` | retired from live mailroom | sorter emits `unknown` |

HF reports (`scripts/run_hf_pilot.py`) include the honesty table plus a
**Local eval packs** section so n=0 classes cannot grow a fake Hub accuracy.
