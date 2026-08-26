# observability/

Tracing, scoring, and evaluation plumbing for the mailroom pipeline.

## Where things live (post KANBAN-061)

The field-scoring implementation is **owned by the shared package**
[`llm-dojo-scoring`](https://github.com/Exios66/llm-dojo-scoring) (v0.9.0+).
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

`get_suite("intake")` (dojo PR #5) scores the pre-sorter clerk against gold
(`intake_prep_completeness`, changed/messy rates, hyphen/blank counts). That
path returns a dict, not an `ExtractionScoreResult` — see
`suite_scoring.score_and_log_intake`.

## Honesty gaps (dojo 0.9.0)

`observability/honest_gaps.py` reads `honest_gap` / `in_corpus` / `retired`
from `get_suite(doc_class)` and attaches a slim block as **trace metadata**
(never tags — tags are immutable/upfront; never a SCORE_CONFIG name that is
not in the registry):

| Class | Gap |
| --- | --- |
| `insurance_claim` | determination-consistency scorers pending; local `coverage_determination` ↔ `denial_reasons` invariant only |
| `compliance_filing` | zero Hub rows; HF pilot excludes the class |
| `corporate_record` | 39 Hub subclass rows; no external extraction benchmark |
| `court_opinion` / `due_diligence` | retired from live mailroom; sorter emits `unknown` |

HF reports (`scripts/run_hf_pilot.py`) include the same table so n=0 classes
cannot grow a fake accuracy.
