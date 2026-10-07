# CUAD contracts

`contract` · **600 rows** (18.2%) · 26 strata · train 540 / test 60 · **CC BY 4.0** (CUAD) plus US public domain (91 EDGAR EX-10).

Canonical card: [`docs/dataset-cards/cuad-contracts.md`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/dataset-cards/cuad-contracts.md) in Mailroom-Corpus-EDA.

## What CUAD is

[CUAD v1](https://www.atticusprojectai.org/cuad) (Contract Understanding Atticus Dataset) is an expert-annotated corpus of US commercial contracts from The Atticus Project, published at NeurIPS 2021. Each contract is labeled for **41 clause types** (parties, dates, governing law, liability caps, license grants, non-competes, …).

In `mailroom-dataset` this is the **contract backbone**:

* 509 rows are a byte-verified export of [`mailroom-cuad-contracts-full`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-cuad-contracts-full) (`metadata.source = cuad_v1`).
* 91 v9 rows are SEC EDGAR EX-10 exhibits (`cuad_clause_labels = {}`).
* `expected_subclass` keeps CUAD's commercial-contract grouping (26 of 28 groups appear).
* Clause gold lives only on `ground_truth` as `cuad_clause_labels` (clause name → `[{text, start}]`).

**13,753 / 13,753 answer spans match `doc_text` at exact character offsets** (P1 integrity, 100%). Mean ~27 spans per annotated contract.

## Purpose in the mailroom

1. Gold `contract` labels for the sorter.
2. Fine-grained contract-type head (`expected_subclass`).
3. Clause-level extraction GT for entity-extraction scoring — never on the blind config.
4. Mid-length legal text between short correspondence/claims and merger agreements.

## Clause density (EDA figures 08–12)

Most-annotated clauses on the 509 CUAD-v1 contracts: `Document Name` (509, mean 1.0 spans), `Parties` (508, mean 5.0), `Agreement Date` (469), `Governing Law` (436). Annotation density is computed over those 509 rows, not the 91 EX-10 exhibits.

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/08_cuad_clause_presence.png" alt="CUAD clause presence across annotated contracts"><figcaption><p>Clause presence (figure 08).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/09_cuad_span_counts.png" alt="CUAD span counts per contract"><figcaption><p>Span counts (figure 09).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/10_cuad_top_clauses.png" alt="Most frequent CUAD clause types"><figcaption><p>Top clauses (figure 10).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/11_cuad_cooccurrence.png" alt="CUAD clause co-occurrence matrix"><figcaption><p>Clause co-occurrence (figure 11).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/12_cuad_spans_distribution.png" alt="Distribution of CUAD span counts"><figcaption><p>Spans distribution (figure 12).</p></figcaption></figure>

Tables: [`cuad_clause_stats.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/cuad_clause_stats.csv), [`cuad_cooccurrence_matrix.csv`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/tables/cuad_cooccurrence_matrix.csv).

## Attribution

Hendrycks, Dan, Collin Burns, Anya Chen, and Spencer Ball. “CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review.” *NeurIPS Datasets and Benchmarks*, 2021. <https://arxiv.org/abs/2103.06268>

## Caveats

* English US commercial contracts only; 26 of 28 CUAD groups appear.
* Seven contract strata have **zero test rows** under the family hash split (see [Classes and strata](../classes-and-strata.md)).
* Treat `cuad_clause_labels` as gold for span-matching against CUAD's convention, not as universal legal truth.
* EX-10 rows are source-native EDGAR exhibits without Atticus clause annotations.

Strata table: [Classes and strata](../classes-and-strata.md#contract--600-rows-182).
