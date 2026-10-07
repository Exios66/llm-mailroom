# Source corpora

Five independently licensed sources make up `mailroom-dataset`. Each has a dataset card in Mailroom-Corpus-EDA (`docs/dataset-cards/`). The pages under this heading summarize those cards for GitBook; the cards remain canonical.

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/27_source_proportions.png" alt="Proportion of mailroom-dataset rows by source corpus"><figcaption><p>Source proportions (figure 27). Interactive: <a href="https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/figures_interactive/27_source_proportions.html">Plotly HTML</a>.</p></figcaption></figure>

| Class              |  Rows | Source page                                         | Producing repo                                                                                   | License                      |
| ------------------ | ----: | --------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------- |
| `contract`         |   600 | [CUAD contracts](cuad-contracts.md)                 | Atticus CUAD + EDGAR EX-10                                                                       | CC BY 4.0 / US public domain |
| `merger_agreement` |   152 | [MAUD merger agreements](maud-merger-agreements.md) | Atticus MAUD                                                                                     | CC BY 4.0                    |
| `corporate_record` |   450 | [SEC corporate records](edgar-corporate-records.md) | EDGAR S-1 / 8-K exhibits                                                                         | US public domain             |
| `correspondence`   | 1,000 | [Enron correspondence](enron-correspondence.md)     | [Enron-Evaluation-Environment](../../../repository-guides/repos/enron-evaluation-environment.md) | research use                 |
| `insurance_claim`  | 1,100 | [CMS insurance claims](cms-insurance-claims.md)     | [claims-data-eda](../../../repository-guides/repos/claims-data-eda.md)                           | Apache-2.0 / MIT / CC BY 4.0 |

## Licensing mix

Redistributing or training on the **joined** corpus must honor the strictest applicable term:

* **CC BY 4.0** — CUAD and MAUD require attribution (Hendrycks et al. 2021; Wang et al. 2023).
* **US public domain** — SEC EDGAR exhibits and the 91 EX-10 contracts. Cite EDGAR; do not present copies as official SEC records.
* **Research use + real PII** — Enron correspondence. Conservative handling; no production/consumer reuse of raw names.
* **Synthetic public-use** — CMS DE-SynPUF has no real PHI. Sibling LOBs (GNOTHEIA, BDR, INSURBIAS) carry Apache-2.0 / MIT / CC BY 4.0.

The family never commits the NC-SA Pile of Law compilation. Court opinions are not a live class.

## Who owns the feed

| Concern                                            | Repository                                                                                                                                                                                 |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Correspondence sample from Enron                   | [Enron-Evaluation-Environment](../../../repository-guides/repos/enron-evaluation-environment.md)                                                                                           |
| Insurance claims sample from CMS                   | [claims-data-eda](../../../repository-guides/repos/claims-data-eda.md)                                                                                                                     |
| Full-corpus EDA, dataset cards, Hub upload helpers | [Mailroom-Corpus-EDA](../../../repository-guides/repos/mailroom-corpus-eda.md)                                                                                                             |
| Loading the corpus into the pipeline               | [llm-mailroom](../../../repository-guides/repos/llm-mailroom.md) (`pipeline/hf_corpus_loader.py`)                                                                                          |
| Subset grammar for evals                           | [eval-environment](../../../repository-guides/repos/eval-environment.md)                                                                                                                   |
| Taxonomy terminology (v7 onward)                   | [Digital-Mailroom](../../../repository-guides/repos/digital-mailroom.md) ([v7 taxonomy contract](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/v7-taxonomy.md)) |

<figure><img src="https://raw.githubusercontent.com/Exios66/Mailroom-Corpus-EDA/main/reports/figures/26_filing_date_timeline.png" alt="Filing and document dates across mailroom-dataset sources"><figcaption><p>Filing-date timeline (figure 26). Interactive: <a href="https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/figures_interactive/26_filing_timeline.html">Plotly HTML</a>.</p></figcaption></figure>
