# MAUD merger agreements

`merger_agreement` · **152 rows** (4.6%) · 5 strata · train 135 / test 17 · **CC BY 4.0**.

Canonical card: [`docs/dataset-cards/maud-merger-agreements.md`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/dataset-cards/maud-merger-agreements.md) in Mailroom-Corpus-EDA.

## What MAUD is

[MAUD v1](https://www.atticusprojectai.org/maud/) (Merger Agreement Understanding Dataset) is 152 public-company merger agreements from The Atticus Project, with expert labels across **22 classification tasks** in four categories: Conditions to Closing, Covenants, No-Shop / FTR, and Definitions. The schema follows the ABA 2021 Public Target Deal Points Study. Corpus export: [Zenodo 7500064](https://zenodo.org/records/7500064).

In `mailroom-dataset` this is the **long-document stress class**:

* `metadata.source = maud_v1`.
* `expected_subclass` is consideration type: `all_cash` (57), `other` (57), `all_stock` (24), `mixed_cash_stock` (13), `mixed_cash_stock_election` (**1** — the corpus minimum).
* Gold labels ride `ground_truth` as `maud_clause_labels`. Metadata consistency holds on all 152 rows: `maud_label_count == sum(maud_categories) == upstream count`.
* Mean **16.4** tasks answered per agreement (range 11–20).

## Purpose in the mailroom

1. Gold `merger_agreement` labels (smallest class).
2. Consideration-type subclass head.
3. Multi-task legal-reasoning GT (22 tasks) — never on the blind config.
4. Context-window stress: mean **89,049 characters** (~22k tokens at chars/4); max **252,135** characters (~63k tokens). These rows exceed common 32k/65k contexts and are why the token-budget table has a 131k column.

## Task coverage (EDA figures 13–15)

Three tasks are annotated on every agreement: Accuracy of Target R&W Closing Condition, MAE Definition, Tail Period & Acquisition Proposal Details. Ordinary-course covenant is on 151/152.

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/13_maud_task_frequency.png" alt="MAUD task frequency across 152 merger agreements"><figcaption><p>Task frequency (figure 13).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/14_maud_answer_distribution.png" alt="MAUD answer distribution"><figcaption><p>Answer distribution (figure 14).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/15_maud_category_coverage.png" alt="MAUD category coverage"><figcaption><p>Category coverage (figure 15).</p></figcaption></figure>

Table: [`maud_task_stats.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/maud_task_stats.csv).

## Attribution

Wang, Steven H., et al. “MAUD: An Expert-Annotated Legal NLP Dataset for Merger Agreement Understanding.” *EMNLP 2023*. <https://arxiv.org/abs/2301.00876> · Zenodo DOI `10.5281/zenodo.7500064`.

## Caveats

* Public-target deals only; private-target structures are out of distribution.
* `mixed_cash_stock_election` has a single train row and **zero test** representation.
* Tasks below ~25% coverage are weak supervision targets.
* Most consumer model contexts cannot ingest these rows whole — plan for 131k+ context, retrieval, or chunked scoring.

Strata table: [Classes and strata](../classes-and-strata.md#merger_agreement--152-rows-46). Length geometry: [EDA reports](../eda-reports.md#text-and-token-geometry).
