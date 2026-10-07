# llm-dojo-scoring

**The shared scoring library. The one place the constellation defines what "correct" means.**

| | |
| :--- | :--- |
| Repository | [Exios66/llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring) |
| Monorepo path | `packages/llm-dojo-scoring` |
| Latest | v0.19.1 as of 2026-10-07 (llm-mailroom pins v0.19.1; entity-extraction and agent-mailroom pin v0.16.0) |
| Used by | llm-mailroom, llm-entity-extraction, eval-environment, local-mailroom-sandbox, agent-mailroom |

## What it does

llm-dojo-scoring replaced each project's local scoring code with one importable library, so a number means the same thing in every repo. It scores extraction field by field according to the field's type, scores classification with proper per-class statistics and confidence intervals, and adds error analysis, cost accounting and reporting on top.

| Field type | How it is scored |
| :--- | :--- |
| `id` | Normalize, then exact match |
| `date` | Parse to ISO, then exact match ("March 3, 2024" equals "03/03/2024") |
| `money` | Strip symbols, compare within one cent |
| `name` | Jaro-Winkler plus token-set ratio |
| `free_text` | SQuAD-style token F1 |
| `entity_list` | Hungarian bipartite matching, then precision, recall and F1 |

An optional embedding similarity "rescues" names and free text that are worded differently but mean the same thing. A blank prediction is never rescued.

## Quick start

```bash
pip install "llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.19.1"
pip install -e ".[embeddings]"   # optional extras: embeddings, tracing, dev, all
```

```python
import llm_dojo_scoring as dojo

suite = dojo.get_suite("insurance_claims_specialist")   # one suite per pipeline agent
result = suite.score(expected_fields, predicted_fields)

ci = dojo.bootstrap_ci([1.0, 0.0, 1.0, 1.0])
```

Command-line tools: `dojo-analyze` (report on a results workbook), `dojo-export` (rebuild workbooks from an experiment log), `dojo-sync` (pull live runs from Langfuse or Phoenix).

## Key modules

| Module | Purpose |
| :--- | :--- |
| `suites` | A dedicated scoring suite per pipeline agent. The API consumers should call. |
| `field_scoring` | Field-type-aware extraction scoring |
| `classification` | Per-class precision, recall, F1/F2, confusion matrices |
| `tasks` | `score_task(...)`: one entry point for every classification task (subtype, docclass, MAUD, Enron, LegalBench, chained, transcription WER) |
| `registry` | Every metric name with its tier, T0 (headline) to T3 (log only) |
| `profiles` | Scoring identity for each of the 26 agent profiles |
| `archive` | The archivist scoring block and audit-row hash, byte-identical to llm-mailroom's |
| `gt_metadata` | Parses the Hub `gt_fields` labels and scopes them per document type |
| `bootstrap`, `cost`, `diagnostics`, `failure_modes` | Confidence intervals, cost estimates, run diagnostics, failure taxonomy |
| `prompts` | Importable prompt catalog, including the frozen eval-environment v1 lineage |

## How it is consumed

llm-mailroom pins it as a git dependency. When dojo publishes a release, a workflow in llm-mailroom (`bump-dojo-scoring.yml`) opens a pull request that bumps the pin. Inside the monorepo, every package resolves it to the workspace copy instead.

## Its documentation

- [README](https://github.com/Exios66/llm-dojo-scoring/blob/main/README.md)
- [docs/SCORING.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/SCORING.md) — scoring methodology
- [docs/EXTRACTION_SCHEMAS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/EXTRACTION_SCHEMAS.md) — the live extraction schemas for the five classes
- [docs/METRIC_IDS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/METRIC_IDS.md) — metric identity per document class
- [docs/SCORECARD_HONESTY.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/SCORECARD_HONESTY.md) — what a scorecard can and cannot claim
- [docs/GT_METADATA.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/GT_METADATA.md) — ground-truth parsing
- [docs/MAUD_LABELS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/MAUD_LABELS.md) — MAUD answer classes
- [docs/ARCHIVE_SCORING.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/ARCHIVE_SCORING.md) — archive scoring block
- [docs/GRID_REPORTS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/GRID_REPORTS.md) — specialist grid reports for GPU studies
- [docs/PROMPTS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/PROMPTS.md) — prompt catalog
- [docs/MIGRATION.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/MIGRATION.md) — upgrading between versions
- [CHANGELOG.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/CHANGELOG.md)
