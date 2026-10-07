# EDA reports

Mailroom-Corpus-EDA profiles the 3,302-document corpus with `run_all.py` (phases P0–P6). This page is the GitBook narrative of that run. Canonical write-up: [`reports/SUMMARY_REPORT.md`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/SUMMARY_REPORT.md) (generated 2026-09-13). Charts: [Visualizations](visualizations.md). Repo guide: [Mailroom-Corpus-EDA](../repository-guides/repos/mailroom-corpus-eda.md). Live gallery: [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/).

```bash
git clone https://github.com/Exios66/Mailroom-Corpus-EDA.git
cd Mailroom-Corpus-EDA
pip install -r requirements.txt
python run_all.py                 # P0–P6
python run_all.py --phases P3 P4  # figures only
```

## Pipeline

| Phase | What                                          | Output                                        |
| ----- | --------------------------------------------- | --------------------------------------------- |
| P0    | Download and manifest validation              | `data/parquet/`                               |
| P1    | Structural integrity and provenance audit     | `reports/tables/integrity_report.json`        |
| P2    | Composition: strata, imbalance, provenance    | `strata_counts.csv`, `imbalance_metrics.json` |
| P3    | Static PNG figures and tables                 | `reports/figures/`, `reports/tables/`         |
| P4    | Interactive Plotly figures                    | `reports/figures_interactive/`                |
| P5    | Cast-safe JSONL and parquet staging           | `data/staging/`                               |
| P6    | Correspondence intent coverage and provenance | `reports/SUMMARY_REPORT.json`                 |

## Executive findings

The corpus is fully joinable (blind ↔ ground\_truth, 3,302/3,302 filename-set agreement). The split rule is byte-exact (zero mismatches). All CUAD annotation offsets validate (13,753/13,753 = 100%).

| Class              |  Rows | Share | Provenance (EDA)              |
| ------------------ | ----: | ----: | ----------------------------- |
| `insurance_claim`  | 1,100 | 33.3% | CMS DE-SynPUF, BDR, INSURBIAS |
| `correspondence`   | 1,000 | 30.3% | `cmu_enron_maildir`           |
| `contract`         |   600 | 18.2% | `cuad_v1` (+ EX-10)           |
| `corporate_record` |   450 | 13.6% | `edgar_s1`                    |
| `merger_agreement` |   152 |  4.6% | `maud_v1`                     |

Imbalance: **7.2×** at type level, **557×** at stratum level. Type entropy = 2.09 bits. Composition tables: [Classes and strata](classes-and-strata.md).

## Integrity (P1)

From [`integrity_report.json`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/integrity_report.json):

* Blind and GT both have 3,302 unique filenames; sets equal; splits 2,979/323 on both; `split_agreement: true`.
* Rule `md5(filename utf-8) % 10 == 0 → test`: **0 mismatches** on `default` and `ground_truth`.
* Claims source used `md5(record_id)` for its own placement; **96/600** CMS-origin rows moved when the family filename rule was applied. The mailroom `split` column wins.
* JSONL vs parquet: 3,302 rows, `doc_text` byte-equal rate 1.0, expected-label equal rate 1.0.
* Blind columns: `filename`, `doc_text`, `prompt`, `metadata`, `split`. `prompt` is empty on every row. `doc_text` has zero nulls.
* CUAD offsets: 509 labeled rows, 13,753 spans, match rate 1.0, mean 27.0 spans/contract.
* MAUD: 152 labeled rows, 22 distinct tasks, 11–20 labels/row (mean 16.4), metadata-count consistency 152/152.

## Text and token geometry (P3 figures 04–07)

Character lengths from [`text_length_stats_by_type.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/text_length_stats_by_type.csv) (heuristic tokens ≈ chars / 4):

| Class              | Mean chars |    p50 |     p95 |     Max | Mean tokens (÷4) |
| ------------------ | ---------: | -----: | ------: | ------: | ---------------: |
| `merger_agreement` |     89,049 | 84,547 | 124,430 | 252,135 |           22,262 |
| `contract`         |     12,290 |  7,414 |  39,799 | 154,004 |            3,073 |
| `corporate_record` |      9,462 |  2,295 |  66,087 | 157,740 |            2,366 |
| `insurance_claim`  |        692 |    288 |   2,921 |   5,678 |              173 |
| `correspondence`   |        526 |    264 |   1,490 |  26,209 |              131 |

Merger agreements **exceed common 32k/65k contexts**. Insurance claims are uniformly short.

Token-budget coverage ([`token_budget_coverage.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/token_budget_coverage.csv)):

|  Budget | Documents | Share |
| ------: | --------: | ----: |
|   4,096 |     2,530 | 76.6% |
|   8,192 |     2,737 | 82.9% |
|  16,384 |     2,958 | 89.6% |
|  32,768 |     3,078 | 93.2% |
|  65,536 |     3,137 | 95.0% |
| 131,072 |     3,295 | 99.8% |
| 200,000 |     3,300 | 99.9% |

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/04_text_length_violin.png" alt="Text-length violin plot by document class"><figcaption><p>Length violin (figure 04).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/05_token_budget_coverage.png" alt="Share of documents that fit successive token budgets"><figcaption><p>Token-budget coverage (figure 05).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/06_text_length_ecdf.png" alt="ECDF of document text length by class"><figcaption><p>Length ECDF (figure 06).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/07_length_by_subclass.png" alt="Text length by subclass"><figcaption><p>Length by subclass (figure 07).</p></figcaption></figure>

## Per-class EDA

| Block                                                | Figures | Report page                                                        |
| ---------------------------------------------------- | ------- | ------------------------------------------------------------------ |
| CUAD clauses (509 contracts, 41 types, 13,753 spans) | 08–12   | [CUAD contracts](source-corpora/cuad-contracts.md)                 |
| MAUD tasks (152 agreements, 22 tasks)                | 13–15   | [MAUD merger agreements](source-corpora/maud-merger-agreements.md) |
| Insurance (1,100 rows, six LOBs)                     | 16–19   | [CMS insurance claims](source-corpora/cms-insurance-claims.md)     |
| Correspondence (1,000 rows, intent 100% hydrated)    | 20–22   | [Enron correspondence](source-corpora/enron-correspondence.md)     |
| Imbalance and minority strata                        | 23–25   | [Classes and strata](classes-and-strata.md)                        |
| Temporal / source / metadata                         | 26–30   | [Source corpora](source-corpora/), below                           |

## Metadata structure (figures 03, 29–30)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/03_metadata_fill_rate_heatmap.png" alt="Metadata field fill-rate heatmap by document class"><figcaption><p>Metadata fill-rate heatmap (figure 03). Table: <a href="https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/metadata_coverage_by_type.csv">metadata_coverage_by_type.csv</a>.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/29_metadata_correlation.png" alt="Correlation among metadata fields"><figcaption><p>Metadata correlation (figure 29).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/30_metadata_cardinality.png" alt="Cardinality of metadata fields"><figcaption><p>Metadata cardinality (figure 30). Interactive: <a href="https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/figures_interactive/30_metadata_heatmap.html">Plotly HTML</a>.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/28_date_span_by_type.png" alt="Document date span by class"><figcaption><p>Date span by type (figure 28).</p></figcaption></figure>

## Tables

All under [`reports/tables/`](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/reports/tables):

| File                                                    | Contents                                      |
| ------------------------------------------------------- | --------------------------------------------- |
| `integrity_report.json`                                 | P1 join, split, CUAD offsets, MAUD counts     |
| `imbalance_metrics.json`                                | Type/stratum ratios, entropy, subclass counts |
| `strata_counts.csv`                                     | 55-cell train/test                            |
| `strata_imbalance_detailed.csv`                         | Per-stratum ratios                            |
| `minority_strata_report.csv`                            | Five cells with < 10 rows                     |
| `text_length_stats_by_type.csv`                         | Length percentiles                            |
| `token_budget_coverage.csv`                             | Context-window fit                            |
| `provenance_by_type.csv`, `provenance_detailed.csv`     | Source mix                                    |
| `temporal_summary.csv`                                  | Date coverage                                 |
| `cuad_clause_stats.csv`, `cuad_cooccurrence_matrix.csv` | Clause gold                                   |
| `maud_task_stats.csv`                                   | 22 MAUD tasks                                 |
| `claim_amount_stats.csv`, `claim_field_coverage.csv`    | Insurance GT                                  |
| `correspondence_topic_intent.csv`                       | Topic × intent                                |
| `metadata_coverage_by_type.csv`                         | Fill rates                                    |

## Audits (EDA `docs/reports/audits/`)

Qualitative / coverage audits sit next to the quantitative P0–P6 run:

* [CUAD EX-10 annotation review](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/reports/audits/cuad_ex10_annotation_review.md) — why 91 EDGAR exhibits have empty `cuad_clause_labels`
* [Docclass coverage matrix](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/reports/audits/docclass_coverage_matrix.md)
* [Docclass expansion priorities](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/reports/audits/docclass_expansion_priorities.md)
* [GT backfill quality](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/reports/audits/gt_backfill_quality.md)

## ML-readiness recommendations (from the EDA summary)

These are analysis notes from Mailroom-Corpus-EDA, not pipeline work items:

1. **Long docs.** `merger_agreement` needs 131k+ context or chunking; most contract text fits 32k.
2. **Minority strata.** Five cells < 10 rows — consider stratification-aware sampling or subclass rollups.
3. **Zero-test strata.** Ten cells have no test rows — a per-stratum test floor belongs in a later corpus revision.
4. **Correspondence multi-task.** Intent is fully hydrated with provenance; sentiment and topic are ready auxiliary heads.
5. **Claims.** Six LOB subtypes; synthetic-data and PAID-only caveats apply to the health block.

## Hub helpers (this repo owns publish)

Upload tooling that used to live in llm-entity-extraction is centralized in Mailroom-Corpus-EDA: `hf_interface.py`, `dataset_export.py`, `docclass_uploader.py` (blind-label strip + leak guard), `intent_backfill.py`, `token_budget.py`. Frozen v8 scripts are under `scripts/archive/v8/`.
