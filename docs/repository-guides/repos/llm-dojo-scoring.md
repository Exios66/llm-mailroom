# llm-dojo-scoring

**The shared scoring library. The one place the constellation defines what "correct" means.**

| | |
| :--- | :--- |
| Repository | [Exios66/llm-dojo-scoring](https://github.com/Exios66/llm-dojo-scoring) |
| Monorepo path | `packages/llm-dojo-scoring` |
| Latest | v0.29.0 as of 2026-11-15, 0.30.0 staged and untagged (llm-mailroom pins 025400f3d703531c5e1a76093e4b803ccc6fb576) |
| Used by | llm-mailroom, eval-environment, local-mailroom-sandbox |

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
pip install "llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@025400f3d703531c5e1a76093e4b803ccc6fb576"
# optional extras (embeddings, tracing, dev, all) come from the same pinned source, not a local path:
# pip install "llm-dojo-scoring[embeddings] @ git+https://github.com/Exios66/llm-dojo-scoring.git@025400f3d703531c5e1a76093e4b803ccc6fb576"
```

```python
import llm_dojo_scoring as dojo

suite = dojo.get_suite("insurance_claims_specialist")   # one suite per pipeline agent
expected_fields = {"claim_number": "CLM-2024-001", "insurer": "Acme Mutual",
                   "claimed_amount": "$12,500.00", "date_filed": "2024-03-02"}
predicted_fields = {"claim_number": "CLM-2024-001", "insurer": "Acme Mutual Insurance",
                    "claimed_amount": "12500", "date_filed": "2024-03-20"}
result = suite.score(expected_fields, predicted_fields)
print(result.field_scores)    # {'claim_number': 1.0, 'insurer': 1.0, 'claimed_amount': 1.0, 'date_filed': 0.67}
print(result.overall_score)   # 0.9175 (run against the pinned Dojo, package 0.30.1)

ci = dojo.bootstrap_ci([1.0, 0.0, 1.0, 1.0])
```

Command-line tool: `dojo-version` (release awareness). The workbook, report and trace-sync CLIs (`dojo-analyze`, `dojo-export`, `dojo-rescore`, `dojo-sync`) were removed in the production slim-down. There is no packaged rescoring CLI. To re-score a stored extraction run, load each document's stored expected and predicted values, call the extraction suite's `get_suite(<agent>).score_document(expected=..., predicted=...)` again (it returns the field-micro precision, recall, F1 and F2 that a bare `score()` call leaves out), and store the new result alongside the old one so the two scorer versions stay comparable (`score_document` accepts extraction suites only; re-score classification runs with `get_suite(<agent>).score(...)`).

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
- [docs/guides/SCORING.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/guides/SCORING.md) — scoring methodology
- [docs/schemas/EXTRACTION_SCHEMAS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/schemas/EXTRACTION_SCHEMAS.md) — the live extraction schemas for the five classes
- [docs/contracts/METRIC_IDS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/contracts/METRIC_IDS.md) — metric identity per document class
- [docs/contracts/SCORECARD_HONESTY.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/contracts/SCORECARD_HONESTY.md) — what a scorecard can and cannot claim
- [docs/schemas/GT_METADATA.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/schemas/GT_METADATA.md) — ground-truth parsing
- [docs/schemas/MAUD_LABELS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/schemas/MAUD_LABELS.md) — MAUD answer classes
- [docs/contracts/ARCHIVE_SCORING.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/contracts/ARCHIVE_SCORING.md) — archive scoring block
- [docs/guides/PROMPTS.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/guides/PROMPTS.md) — prompt catalog
- [docs/guides/MIGRATION.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/docs/guides/MIGRATION.md) — upgrading between versions
- [CHANGELOG.md](https://github.com/Exios66/llm-dojo-scoring/blob/main/CHANGELOG.md)
