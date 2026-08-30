# observability/

Tracing, scoring, and evaluation plumbing for the mailroom pipeline.

## Where things live (post KANBAN-061)

The field-scoring implementation is **owned by the shared package**
[`llm-dojo-scoring`](https://github.com/Exios66/llm-dojo-scoring) (v0.12.2).
This repo keeps only a backward-compatibility shim.

| Module | Status |
| --- | --- |
| `tracing.py`, `langfuse_setup.py`, `phoenix_setup.py` | local — tracing facade/backends |
| `scores.py` | local — Langfuse score configs; names validated against the dojo metric registry at import |
| `classification_scoring.py` | local — exact class match (MAUD ≠ CUAD) |
| `field_scoring.py` | **deprecated shim** over `llm_dojo_scoring.field_scoring` |
| `specialist_suites.py` | local — one dedicated scoring suite per live extract class |
| `extraction_gt.py` / `posthoc_gt.py` | local — Hub catalog labels + post-hoc schema GT |

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

**Production first-pass (`success_rate`, gt=none):** `emit_pipeline_scores`
always attaches this registered 0/1 flag. 1 means the document archived in
one hop (no retry / Lane A / arbiter / boss / human review / guardrail /
transient self-loop). Incoming live documents are zero-shot; the flag does
not consult `class_correct` or field GT. The-Mailroom tiles FIRST PASS from
it; Langfuse Performance dashboard charts live+pilot rate and count.

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

## Dedicated specialist extraction suites

Every live specialist has a dedicated scoring suite (`observability.specialist_suites`):

| Specialist | Classes scored | Suite extras |
| --- | --- | --- |
| `contracts_specialist` | `contract` (CUAD) | field-micro F1; CUAD family / clauses |
| `contracts_specialist` | `merger_agreement` (MAUD) | MAUD question extras; same agent, rebound suite |
| `corporate_records_specialist` | `corporate_record` | typed field-micro + entity-list F1 |
| `correspondence_specialist` | `correspondence` | Enron topic/sentiment extras when Hub has them |
| `compliance_specialist` | `compliance_filing` | typed field-micro (local pack; zero Hub rows) |
| `insurance_claims_specialist` | `insurance_claim` | determination_consistency / amount_exactness |

`merger_agreement` does **not** add a sixth specialist agent — extraction still
runs through `contracts_specialist`. The scoring suite is rebound (MAUD
consideration subclasses, MAUD extras) so CUAD families never score MAUD.

Hub official labels still win. Remaining schema fields are filled post-hoc from
document text (`observability.posthoc_gt`) so every included document has
scorable expected_fields. Provenance (`n_hub` / `n_posthoc`) is recorded; a
post-hoc fill is never billed as an official Hub annotation.
`compliance_filing` stays out of Hub `--real` (n=0).

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
| `corporate_record` | 39 Hub subclass rows; no *external* extraction benchmark | Post-hoc schema GT from exhibit text is scored on Hub rows (not claimed as CUAD-grade gold); local schema-complete pack remains the mock/check self-check |
| `court_opinion` / `due_diligence` | retired from live mailroom | sorter emits `unknown` |

HF reports (`scripts/run_hf_pilot.py`) include the honesty table plus a
**Local eval packs** section so n=0 classes cannot grow a fake Hub accuracy.
