# Visualizations

Static PNGs and interactive Plotly charts from [Exios66/Mailroom-Corpus-EDA](https://github.com/Exios66/Mailroom-Corpus-EDA). Images are the tracked files on that repo's `main`; this page is the GitBook gallery. Narrative and tables: [EDA reports](eda-reports.md). Dataset overview: [Mailroom dataset](mailroom-dataset.md).

Regenerate with `python run_all.py --phases P3 P4` in Mailroom-Corpus-EDA. Figure numbers match `reports/figures/` and `reports/SUMMARY_REPORT.md`.

PNG base: `https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/`. Interactive HTML: GitHub Pages copies of [`reports/figures_interactive/`](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/reports/figures_interactive) (18 files). Site: [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/).

{% hint style="info" %}
GitBook strips page scripts, so Plotly charts run inside iframes pointed at `exios66.github.io`. If a frame is blank, open the linked HTML.
{% endhint %}

## Live dashboard

The Mailroom-Corpus-EDA site (composition bar, Plotly grid, 30-figure gallery, tables, and the narrative summary). Open it full-page: [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/).

{% embed url="https://exios66.github.io/Mailroom-Corpus-EDA/" %}
mailroom-dataset Corpus — EDA Dashboard
{% endembed %}

## Composition and coverage (01–03)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/01_type_and_subclass_distribution.png" alt="Document type and subclass distribution"><figcaption><p>01 — Type and subclass distribution.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/02_strata_train_test.png" alt="Train versus test counts per stratum"><figcaption><p>02 — Strata train/test.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/03_metadata_fill_rate_heatmap.png" alt="Metadata fill-rate heatmap by class"><figcaption><p>03 — Metadata fill-rate heatmap.</p></figcaption></figure>

## Text length and token budgets (04–07)

Interactive: [04 violin](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/04_text_length_violin.html) · [05 budgets](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/05_token_budget_coverage.html) · [06 ECDF](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/06_text_length_ecdf.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/04_text_length_violin.png" alt="Text-length violin by document class"><figcaption><p>04 — Length violin. Merger agreements sit far above the other classes.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/05_token_budget_coverage.png" alt="Token budget coverage bars"><figcaption><p>05 — Token-budget coverage (76.6% ≤4k, 93.2% ≤32k, 99.8% ≤131k).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/06_text_length_ecdf.png" alt="ECDF of text length by class"><figcaption><p>06 — Length ECDF.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/07_length_by_subclass.png" alt="Text length by subclass"><figcaption><p>07 — Length by subclass.</p></figcaption></figure>

## CUAD contracts (08–12)

Interactive: [08 presence](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/08_cuad_clause_presence.html) · [09 spans](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/09_cuad_span_counts.html) · [10 top clauses](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/10_cuad_top_clauses.html) · [11 co-occurrence](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/11_cuad_cooccurrence.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/08_cuad_clause_presence.png" alt="CUAD clause presence"><figcaption><p>08 — Clause presence (509 annotated contracts).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/09_cuad_span_counts.png" alt="CUAD span counts"><figcaption><p>09 — Span counts.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/10_cuad_top_clauses.png" alt="Top CUAD clause types"><figcaption><p>10 — Top clauses.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/11_cuad_cooccurrence.png" alt="CUAD clause co-occurrence"><figcaption><p>11 — Clause co-occurrence.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/12_cuad_spans_distribution.png" alt="CUAD spans distribution"><figcaption><p>12 — Spans distribution (13,753 spans, 100% offset match).</p></figcaption></figure>

Write-up: [CUAD contracts](source-corpora/cuad-contracts.md).

## MAUD merger agreements (13–15)

Interactive: [13 frequency](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/13_maud_task_frequency.html) · [14 answers](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/14_maud_answer_distribution.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/13_maud_task_frequency.png" alt="MAUD task frequency"><figcaption><p>13 — Task frequency (22 tasks, 152 agreements).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/14_maud_answer_distribution.png" alt="MAUD answer distribution"><figcaption><p>14 — Answer distribution.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/15_maud_category_coverage.png" alt="MAUD category coverage"><figcaption><p>15 — Category coverage.</p></figcaption></figure>

Write-up: [MAUD merger agreements](source-corpora/maud-merger-agreements.md).

## Insurance claims (16–19)

Interactive: [16 amounts](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/16_claim_amount.html) · [17 coverage](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/17_coverage_determination.html) · [18 dates](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/18_claim_dates.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/16_claim_amount_distribution.png" alt="Claim amount distribution"><figcaption><p>16 — Claim amounts (median $1,250 on 1,062 populated rows).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/17_coverage_determination.png" alt="Coverage determination"><figcaption><p>17 — Coverage determination.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/18_claim_dates_timeline.png" alt="Claim dates timeline"><figcaption><p>18 — Loss-to-filing dates.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/19_claim_subtype_fields.png" alt="Claim subtype field fill"><figcaption><p>19 — Subtype field fill (six LOBs).</p></figcaption></figure>

Write-up: [CMS insurance claims](source-corpora/cms-insurance-claims.md).

## Correspondence (20–22)

Interactive: [20 topic × intent](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/20_corr_topic_intent.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/20_corr_content_topic.png" alt="Correspondence content topics"><figcaption><p>20 — Content topic.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/21_corr_intent.png" alt="Correspondence intent classes"><figcaption><p>21 — Intent (1,000/1,000 hydrated).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/22_corr_sentiment.png" alt="Correspondence sentiment"><figcaption><p>22 — Sentiment.</p></figcaption></figure>

Write-up: [Enron correspondence](source-corpora/enron-correspondence.md).

## Imbalance (23–25)

Interactive: [23 treemap](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/23_imbalance_treemap.html) · [24 ratios](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/24_strata_ratio.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/23_imbalance_treemap.png" alt="Imbalance treemap"><figcaption><p>23 — Imbalance treemap (7.2× type, 557× stratum).</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/24_strata_imbalance_ratio.png" alt="Strata imbalance ratios"><figcaption><p>24 — Strata imbalance ratios.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/25_minority_strata.png" alt="Minority strata"><figcaption><p>25 — Minority strata (five cells, 19 rows).</p></figcaption></figure>

Write-up: [Classes and strata](classes-and-strata.md).

## Provenance, time, metadata (26–30)

Interactive: [26 timeline](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/26_filing_timeline.html) · [27 sources](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/27_source_proportions.html) · [30 heatmap](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/30_metadata_heatmap.html)

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/26_filing_date_timeline.png" alt="Filing date timeline"><figcaption><p>26 — Filing-date timeline.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/27_source_proportions.png" alt="Source corpus proportions"><figcaption><p>27 — Source proportions.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/28_date_span_by_type.png" alt="Date span by document class"><figcaption><p>28 — Date span by type.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/29_metadata_correlation.png" alt="Metadata field correlation"><figcaption><p>29 — Metadata correlation.</p></figcaption></figure>

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/30_metadata_cardinality.png" alt="Metadata field cardinality"><figcaption><p>30 — Metadata cardinality.</p></figcaption></figure>

## Interactive charts

Plotly HTML (hover/zoom) from `reports/figures_interactive/`, served on GitHub Pages. Each iframe is the same file the dashboard embeds.

[04\_text\_length\_violin.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/04_text_length_violin.html)

[05\_token\_budget\_coverage.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/05_token_budget_coverage.html)

[06\_text\_length\_ecdf.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/06_text_length_ecdf.html)

[08\_cuad\_clause\_presence.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/08_cuad_clause_presence.html)

[09\_cuad\_span\_counts.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/09_cuad_span_counts.html)

[10\_cuad\_top\_clauses.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/10_cuad_top_clauses.html)

[11\_cuad\_cooccurrence.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/11_cuad_cooccurrence.html)

[13\_maud\_task\_frequency.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/13_maud_task_frequency.html)

[14\_maud\_answer\_distribution.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/14_maud_answer_distribution.html)

[16\_claim\_amount.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/16_claim_amount.html)

[17\_coverage\_determination.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/17_coverage_determination.html)

[18\_claim\_dates.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/18_claim_dates.html)

[20\_corr\_topic\_intent.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/20_corr_topic_intent.html)

[23\_imbalance\_treemap.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/23_imbalance_treemap.html)

[24\_strata\_ratio.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/24_strata_ratio.html)

[26\_filing\_timeline.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/26_filing_timeline.html)

[27\_source\_proportions.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/27_source_proportions.html)

[30\_metadata\_heatmap.html](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/30_metadata_heatmap.html)

| File                               | Pair PNG | GitHub Pages                                                                                               |
| ---------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `04_text_length_violin.html`       | 04       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/04_text_length_violin.html)       |
| `05_token_budget_coverage.html`    | 05       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/05_token_budget_coverage.html)    |
| `06_text_length_ecdf.html`         | 06       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/06_text_length_ecdf.html)         |
| `08_cuad_clause_presence.html`     | 08       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/08_cuad_clause_presence.html)     |
| `09_cuad_span_counts.html`         | 09       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/09_cuad_span_counts.html)         |
| `10_cuad_top_clauses.html`         | 10       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/10_cuad_top_clauses.html)         |
| `11_cuad_cooccurrence.html`        | 11       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/11_cuad_cooccurrence.html)        |
| `13_maud_task_frequency.html`      | 13       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/13_maud_task_frequency.html)      |
| `14_maud_answer_distribution.html` | 14       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/14_maud_answer_distribution.html) |
| `16_claim_amount.html`             | 16       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/16_claim_amount.html)             |
| `17_coverage_determination.html`   | 17       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/17_coverage_determination.html)   |
| `18_claim_dates.html`              | 18       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/18_claim_dates.html)              |
| `20_corr_topic_intent.html`        | 20–21    | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/20_corr_topic_intent.html)        |
| `23_imbalance_treemap.html`        | 23       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/23_imbalance_treemap.html)        |
| `24_strata_ratio.html`             | 24       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/24_strata_ratio.html)             |
| `26_filing_timeline.html`          | 26       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/26_filing_timeline.html)          |
| `27_source_proportions.html`       | 27       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/27_source_proportions.html)       |
| `30_metadata_heatmap.html`         | 30       | [open](https://exios66.github.io/Mailroom-Corpus-EDA/figures_interactive/30_metadata_heatmap.html)         |
