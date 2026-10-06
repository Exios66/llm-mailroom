# EDA visuals

All 30 static PNG figures from [Exios66/Mailroom-Corpus-EDA](https://github.com/Exios66/Mailroom-Corpus-EDA). Images are the GitHub Pages copies of `reports/figures/` (generated 2026-09-13 by `run_all.py` P3). Parent page: [Data and corpora](data-and-corpora.md). Plotly versions: [Interactive charts](eda-interactive.md). Narrative: [reports/SUMMARY_REPORT.md](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/SUMMARY_REPORT.md).

{% hint style="info" %}
These figures profile `Lucius-Morningstar/mailroom-dataset` v1 (corpus family v9, 3,302 documents). The pipeline pin on [Data and corpora](data-and-corpora.md) is Hub tag `v9.1` (`ed7576b6`). Regenerate upstream with `python run_all.py --phases P3`.
{% endhint %}

## Composition and coverage

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/01_type_and_subclass_distribution.png" alt="Document type and subclass distribution across the 3,302-document corpus"><figcaption><p>01 — Type and subclass distribution.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/02_strata_train_test.png" alt="Train versus test counts for all 55 class-by-subclass strata"><figcaption><p>02 — Train/test counts per stratum.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/03_metadata_fill_rate_heatmap.png" alt="Metadata field fill-rate heatmap by document class"><figcaption><p>03 — Metadata fill-rate heatmap.</p></figcaption></figure>

## Length and token budgets

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/04_text_length_violin.png" alt="Text length violin plot by document class"><figcaption><p>04 — Text length by class (merger agreements are the long tail).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/05_token_budget_coverage.png" alt="Token budget coverage across 4k, 16k, 32k, and 128k windows"><figcaption><p>05 — Token-budget coverage (77% ≤4k, 90% ≤16k, 93% ≤32k, 99.8% ≤128k).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/06_text_length_ecdf.png" alt="Empirical CDF of document length by class"><figcaption><p>06 — Length ECDF by class.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/07_length_by_subclass.png" alt="Text length by subclass"><figcaption><p>07 — Length by subclass.</p></figcaption></figure>

## CUAD contracts (509 annotated + 91 EX-10)

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/08_cuad_clause_presence.png" alt="CUAD clause presence across 509 annotated contracts"><figcaption><p>08 — CUAD clause presence (13,753 spans, 100% exact offset match).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/09_cuad_span_counts.png" alt="CUAD span counts per clause type"><figcaption><p>09 — CUAD span counts.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/10_cuad_top_clauses.png" alt="Most-annotated CUAD clause types"><figcaption><p>10 — Top CUAD clause types (Document Name, Parties, Agreement Date, Governing Law).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/11_cuad_cooccurrence.png" alt="CUAD clause co-occurrence matrix"><figcaption><p>11 — CUAD clause co-occurrence.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/12_cuad_spans_distribution.png" alt="Distribution of CUAD span counts"><figcaption><p>12 — CUAD span-count distribution.</p></figcaption></figure>

## MAUD merger agreements (152 × 22 tasks)

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/13_maud_task_frequency.png" alt="MAUD task frequency across 152 merger agreements"><figcaption><p>13 — MAUD task frequency.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/14_maud_answer_distribution.png" alt="MAUD answer distribution by task"><figcaption><p>14 — MAUD answer distribution.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/15_maud_category_coverage.png" alt="MAUD category coverage"><figcaption><p>15 — MAUD category coverage.</p></figcaption></figure>

## Insurance claims (1,100 rows, six LOB subtypes)

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/16_claim_amount_distribution.png" alt="Insurance claim amount distribution"><figcaption><p>16 — Claim amount distribution (median $1,250 on 1,062 rows with an amount).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/17_coverage_determination.png" alt="Coverage determination on insurance claims"><figcaption><p>17 — Coverage determination (fully populated).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/18_claim_dates_timeline.png" alt="Insurance claim dates timeline"><figcaption><p>18 — Claim dates timeline.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/19_claim_subtype_fields.png" alt="Claim field fill by line-of-business subtype"><figcaption><p>19 — Field fill by LOB subtype (13/13 fields, 100% across carrier/inpatient/outpatient/pde/property/auto).</p></figcaption></figure>

## Correspondence (1,000 Enron rows)

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/20_corr_content_topic.png" alt="Correspondence content topics"><figcaption><p>20 — Content topics.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/21_corr_intent.png" alt="Correspondence intent labels, 100 percent hydrated"><figcaption><p>21 — Intent (1,000/1,000 hydrated: 162 aeslc_join, 105 heuristic, 637 llm_zero_shot, 96 manual).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/22_corr_sentiment.png" alt="Correspondence sentiment labels"><figcaption><p>22 — Sentiment.</p></figcaption></figure>

## Imbalance

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/23_imbalance_treemap.png" alt="Class by subclass imbalance treemap"><figcaption><p>23 — Imbalance treemap (7.2× type-level, 557× stratum-level).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/24_strata_imbalance_ratio.png" alt="Stratum imbalance ratios and zero-test strata"><figcaption><p>24 — Stratum imbalance ratios (10 strata have zero test rows).</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/25_minority_strata.png" alt="Minority strata with fewer than 10 rows"><figcaption><p>25 — Minority strata (5 strata &lt; 10 rows, 19 rows total).</p></figcaption></figure>

## Provenance and time

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/26_filing_date_timeline.png" alt="Filing-date timeline"><figcaption><p>26 — Filing-date timeline.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/27_source_proportions.png" alt="Source corpus proportions in mailroom-dataset"><figcaption><p>27 — Source proportions.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/28_date_span_by_type.png" alt="Date span by document class"><figcaption><p>28 — Date span by class.</p></figcaption></figure>

## Metadata structure

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/29_metadata_correlation.png" alt="Metadata field correlation"><figcaption><p>29 — Metadata correlation.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/30_metadata_cardinality.png" alt="Metadata field cardinality"><figcaption><p>30 — Metadata cardinality.</p></figcaption></figure>

## Related

* [Data and corpora](data-and-corpora.md)
* [Interactive charts](eda-interactive.md)
* [Mailroom-Corpus-EDA guide](../repository-guides/repos/mailroom-corpus-eda.md)
* [Live dashboard](https://exios66.github.io/Mailroom-Corpus-EDA/#gallery)
* [reports/figures/](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/reports/figures)
