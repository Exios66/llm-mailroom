# `llm_dojo_scoring` package layout

The package is the shared scoring engine for llm-mailroom and the local-mailroom sandbox.
Its modules are grouped by what they are about. Only the entry points sit at the root.

```
llm_dojo_scoring/
├── __init__.py        public API re-exports (score_extraction, score_task, suites, …)
├── _layout.py         legacy flat import paths → subpackage modules
├── config.py          process-wide settings (LLM_DOJO_SCORING_CONFIG)
├── prompts/           importable prompt catalog and frozen eval-environment lineage
├── schemas/           what the dataset rows mean
├── scoring/           deterministic scorers
├── pipeline/          mailroom pipeline and sandbox contracts
├── metrics/           metric identity and emission
├── analysis/          run analysis and reporting
└── integrations/      trace sinks and release awareness
```

## Where things live

| Subpackage | Module | What it does |
|:---|:---|:---|
| **`schemas/`** | `doc_classes` | The one hand-edited registry of document classes, their extraction fields and field types; every other class/field table derives from it. Stdlib-only leaf |
| | `corpus` | mailroom-dataset alignment: doc types, subclasses, extraction fields, the CMS source-table mapping |
| | `gt_metadata` | Hub `gt_fields` / `gt_presence` parsing into scoring-ready fields |
| | `intents` | Controlled `intent` vocabularies for the classes that carry one |
| | `equivalences` | Subtype and doc-subclass normalization and equivalence |
| | `sorter_guide` | Sorter-facing subclass guide: what each subclass key means |
| | `schema_contract` | Empty-field contract guard for specialist schemas |
| | `cuad_schema` | CUAD categories, families, questions, guided-decoding JSON Schema |
| | `maud_schema`, `maud` | MAUD question→answer contract and answer-class catalogs |
| | `corporate_record_schema`, `corporate_record_relabel` | Per-subclass corporate-record contracts and the deterministic GT repair |
| **`scoring/`** | `field_scoring` | Field-type-aware extraction scoring (date, money, id, name, free text, entity lists) |
| | `field_support` | Exact GT support summaries for the pilot and factuality adapters |
| | `extraction_metrics` | Field-micro P/R/F1/F2 over extraction events |
| | `classification` | Label normalization, exact match, per-class and macro P/R/F1/F2, confusion |
| | `content_scoring` | Enron topic/sentiment and MAUD per-question scorers |
| | `claims_consistency` | Insurance determination consistency and amount exactness |
| | `legalbench_scoring` | LegalBench scoring, computed locally |
| | `asr` | Word and character error rates for transcription/OCR |
| | `agent_rubrics` | Dependency-free isolation-eval rubrics |
| | `tasks` | `score_task`: task-aware scoring across the document hierarchy |
| | `reviewer` | Reviewer-lane scorecard: class/subclass, risk bound, calibration, routing, verdicts, cost |
| | `suites` | Dedicated per-agent scoring suites: the API consumers call |
| | `trace_knobs` | Confidence and reasoning captured as experimental knobs |
| **`pipeline/`** | `mailroom` | The live llm-mailroom pipeline contract |
| | `intake` | Intake clerk: pre-sorter text cleaning and format fixes |
| | `pilot_labels`, `pilot_summary` | Exact pipeline label policies and pilot classification support |
| | `pipeline_scoring`, `pipeline_verdict` | Pipeline classification, lifecycle reports and verdict thresholds |
| | `archive` | Archivist scoring block and the hash-chained `archived` audit row |
| | `sandbox_scoring` | Sandbox MAUD/CUAD rubrics, including the per-field contract truth |
| | `sandbox_schema_adherence` | Sandbox payload quality |
| **`metrics/`** | `registry`, `metric_meta` | Every score name → tier, and its citation/GT metadata |
| | `bundles`, `doc_bundles`, `pruning` | Named metric groupings and the tier filter |
| | `profiles` | One declarative scoring identity per agent |
| | `emitter` | The one routing layer for scores (JSONL manifest, Langfuse sink) |
| | `scorecard_honesty` | Fail-closed GT, metric identity, provenance and completion checks |
| | `score_changelog` | Machine ledger of releases that moved a score |
| **`analysis/`** | `bootstrap`, `calibration`, `diagnostics`, `failure_modes` | Statistics and diagnostics over runs |
| | `calibration` | Exact risk bounds, sample size, ECE/Brier, AUROC, cutoffs |
| | `experiment` | Experiment-log records |
| | `serving`, `cost` | Local-vs-API serving, token cost |
| **`integrations/`** | `update_awareness` | `dojo-version` release awareness |
| | `update_awareness` | Opt-in release discovery behind `dojo-version` |

## Imports

Import from the subpackage path in new code:

```python
from llm_dojo_scoring import score_extraction            # public API, unchanged
from llm_dojo_scoring.scoring.field_scoring import score_field
from llm_dojo_scoring.pipeline.sandbox_scoring import field_value_ok
from llm_dojo_scoring.schemas.corpus import CMS_SOURCE_TABLES
```

Until v0.26.0 every module sat directly under `llm_dojo_scoring`. Those flat paths still work:
`import llm_dojo_scoring.field_scoring`, `from llm_dojo_scoring.sandbox_scoring import …` and
`mock.patch("llm_dojo_scoring.field_scoring.…")` resolve to the **same module object** as the new path
(`_layout.LEGACY_MODULES`, guarded by `tests/test_package_layout.py`).

One thing does change: a module's `__name__` is its new path. Code that identifies Dojo frames or loggers by
module name has to accept the subpackage path. For example, `llm_dojo_scoring.pipeline.pipeline_verdict`
replaces `llm_dojo_scoring.pipeline_verdict`. See [`docs/guides/MIGRATION.md`](../docs/guides/MIGRATION.md).

## Adding a module

1. Put it in the subpackage that matches what it is about. If none fits, discuss a new one; don't add it at the root.
2. Give the module, and each class and function, a docstring (`tests/test_docstring_coverage.py`).
3. Add it to the table above. `tests/test_package_layout.py` fails when a `.py` file appears at the root.
