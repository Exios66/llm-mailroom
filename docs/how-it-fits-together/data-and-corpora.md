# Data and corpora

Every evaluation, training run and pilot in the constellation draws from one dataset family on Hugging Face, published under the [`Lucius-Morningstar`](https://huggingface.co/Lucius-Morningstar) organization. This page explains what is in it, where each part comes from, and the rules for using it.

The charts and dashboard below are the live Mailroom-Corpus-EDA displays — the same figures GitHub Pages serves at [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/). Full static gallery: [EDA visuals](eda-visuals.md). Plotly charts: [Interactive charts](eda-interactive.md).

## The canonical dataset

[`Lucius-Morningstar/mailroom-dataset`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset) is the corpus every repository measures against. It is called "v1" on the Hub and "v9" in the corpus family's lineage; both names refer to the same thing.

| Fact                     | Value                                                                                                                       |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Documents                | 3,302 (2,979 train, 323 test)                                                                                               |
| Document classes         | 5                                                                                                                           |
| Class-by-subclass strata | 55                                                                                                                          |
| Pinned revision          | `ed7576b6` (tag v9.1)                                                                                                       |
| Frozen parent            | [`mailroom-corpus`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-corpus) v8, 2,000 rows, revision `eafe1ab4` |
| EDA run (figures)        | 2026-09-13 · `run_all.py` P0–P6 · [Mailroom-Corpus-EDA](https://github.com/Exios66/Mailroom-Corpus-EDA)                      |

### Live EDA dashboard

The Mailroom-Corpus-EDA site (composition bar, Plotly grid, 30-figure gallery, tables, and the narrative summary). Open it full-page: [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/).

<iframe src="https://exios66.github.io/Mailroom-Corpus-EDA/" title="mailroom-dataset EDA dashboard" width="100%" height="820" loading="lazy"></iframe>

{% embed url="https://exios66.github.io/Mailroom-Corpus-EDA/" %}
mailroom-dataset Corpus — EDA Dashboard (GitHub Pages)
{% endembed %}

### Classes and sources

| Class              |  Rows | Source                                             | License                                                  |
| ------------------ | ----: | -------------------------------------------------- | -------------------------------------------------------- |
| `insurance_claim`  | 1,100 | CMS DE-SynPUF, GNOTHEIA, BDR, INSURBIAS narratives | Apache-2.0 / MIT / CC BY 4.0                             |
| `correspondence`   | 1,000 | Enron de-duplicated corpus                         | research use; contains real names, handle conservatively |
| `contract`         |   600 | CUAD v1 (509) + SEC EDGAR EX-10 (91)               | CC BY 4.0 / US public domain                             |
| `corporate_record` |   450 | SEC EDGAR S-1 and 8-K exhibits                     | US public domain                                         |
| `merger_agreement` |   152 | MAUD v1                                            | CC BY 4.0                                                |

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/01_type_and_subclass_distribution.png" alt="Document type and subclass distribution across the 3,302-document corpus"><figcaption><p>Type and subclass distribution (3,302 documents, 55 strata). Source: Mailroom-Corpus-EDA <code>reports/figures/01_type_and_subclass_distribution.png</code>.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/23_imbalance_treemap.png" alt="Class by subclass imbalance treemap"><figcaption><p>Class × subclass imbalance treemap (7.2× at type level, 557× at stratum level). Source: <code>23_imbalance_treemap.png</code>.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/27_source_proportions.png" alt="Source corpus proportions in mailroom-dataset"><figcaption><p>Source corpus proportions. Source: <code>27_source_proportions.png</code>.</p></figcaption></figure>

### Configs

| Config         |  Rows | Contents                                                                           |
| -------------- | ----: | ---------------------------------------------------------------------------------- |
| `default`      | 3,302 | Document text and metadata only. No labels, by construction.                       |
| `ground_truth` | 3,302 | Labels (`gt_fields`), hashes, the evaluation contract, matter and group membership |
| `bundles`      |    50 | Synthetic multi-document bundles built over real anchors                           |
| `streams`      |    62 | An interleaved ingress stream with distractors                                     |
| `fixtures`     |    32 | Recovery and adversarial fixtures used for calibration                             |

To use it, load `default` and `ground_truth` separately and join on `filename`:

```python
from datasets import load_dataset
blind = load_dataset("Lucius-Morningstar/mailroom-dataset", "default")
gt = load_dataset("Lucius-Morningstar/mailroom-dataset", "ground_truth")
```

The pipeline itself does not depend on the `datasets` library; it loads the corpus through `pipeline/hf_corpus_loader.py`, which also verifies each row's `content_sha256` against its text.

Hub Dataset Viewer for the blind `default` config (train split):

<iframe src="https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset/embed/viewer/default/train" title="Hugging Face Dataset Viewer for mailroom-dataset default/train" width="100%" height="560" loading="lazy"></iframe>

{% embed url="https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset" %}
Lucius-Morningstar/mailroom-dataset on the Hub
{% endembed %}

### Length, tokens, and the train/test split

Merger agreements are the long tail (mean ~89k characters, max ~252k — past common 32k/65k contexts). Insurance claims are uniformly short. Token-budget coverage from the EDA run: 77% ≤4k, 90% ≤16k, 93% ≤32k, 99.8% ≤128k.

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/04_text_length_violin.png" alt="Text length violin plot by document class"><figcaption><p>Text length by class. Source: <code>04_text_length_violin.png</code>.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/05_token_budget_coverage.png" alt="Token budget coverage across 4k, 16k, 32k, and 128k windows"><figcaption><p>Token-budget coverage. Source: <code>05_token_budget_coverage.png</code>.</p></figcaption></figure>

<figure><img src="https://exios66.github.io/Mailroom-Corpus-EDA/figures/02_strata_train_test.png" alt="Train versus test counts for all 55 class-by-subclass strata"><figcaption><p>Train/test counts for all 55 strata (family split rule: <code>md5(filename) % 10 == 0</code> → test). Source: <code>02_strata_train_test.png</code>.</p></figcaption></figure>

The remaining 24 static figures (CUAD, MAUD, claims, correspondence, imbalance, temporal, metadata) are on [EDA visuals](eda-visuals.md). Hover/zoom Plotly versions are on [Interactive charts](eda-interactive.md).

## Rules everyone follows

* **Pin a revision.** Never read the live tip of a dataset. Code records the revision it used so results are reproducible.
* **Keep labels blind.** The model under test only ever sees the `default` config.
* **One split rule.** Across the whole family, `md5(filename) % 10 == 0` means test, so a document is in the same split in every dataset that contains it.
* **Subclasses are strata, not classes.** `expected_subclass` is a second-level label (for example `all_cash` under `merger_agreement`). It is not promoted to a top-level class unless the pipeline's taxonomy adopts it.
* **Retired classes stay retired.** `compliance_filing`, `court_opinion` and `due_diligence` are no longer live classes.

## Other datasets in the family

| Dataset                                             | Role                                                                   |
| --------------------------------------------------- | ---------------------------------------------------------------------- |
| `docclass-pilot`                                    | 48 class-by-subclass examples, one per stratum, for quick pilots       |
| `enron-correspondence-dedup`                        | 247,523-row de-duplicated Enron pool that correspondence is drawn from |
| `mailroom-cuad-contracts-full`                      | Byte-verified CUAD contract mirror                                     |
| `cms-desynpuf-insurance-claims`                     | Rendered CMS EOB documents                                             |
| `mailroom-finetune`, `mailroom-modernbert-training` | Training copies used by mailroom-ml                                    |
| `mailroom-modernbert-classifier`                    | Where mailroom-ml publishes the trained classifier                     |
| `legalbench-full`                                   | LegalBench task pack (not a pipeline-ingest corpus)                    |

## Which repository owns what

| Concern                                          | Repository                                                                                                                                                                           |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Correspondence sample from Enron                 | [Enron-Evaluation-Environment](../repository-guides/repos/enron-evaluation-environment.md)                                                                                           |
| Insurance claims sample from CMS                 | [claims-data-eda](../repository-guides/repos/claims-data-eda.md)                                                                                                                     |
| Full-corpus EDA, dataset cards, upload helpers   | [Mailroom-Corpus-EDA](../repository-guides/repos/mailroom-corpus-eda.md)                                                                                                             |
| Loading the corpus into the pipeline             | [llm-mailroom](../repository-guides/repos/llm-mailroom.md) (`pipeline/hf_corpus_loader.py`)                                                                                          |
| Subset grammar and stratified sampling for evals | [eval-environment](../repository-guides/repos/eval-environment.md) (`src/evals/cases.py`)                                                                                            |
| Taxonomy terminology (v7 onward)                 | [Digital-Mailroom](../repository-guides/repos/digital-mailroom.md) ([v7 taxonomy contract](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/v7-taxonomy.md)) |

The dataset cards (one per source) live in Mailroom-Corpus-EDA under [`docs/dataset-cards/`](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/docs/dataset-cards), and `run_all.py` there reproduces every number on this page. Figures are tracked in that repo under [`reports/figures/`](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/reports/figures) (PNG) and [`reports/figures_interactive/`](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/reports/figures_interactive) (Plotly HTML); this site embeds the GitHub Pages copies so they render in GitBook.
