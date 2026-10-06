# Mailroom-Corpus-EDA

**Full-corpus analysis of the canonical `mailroom-dataset`, its dataset cards, and the central Hugging Face upload helpers.**

| | |
| :--- | :--- |
| Repository | [Exios66/Mailroom-Corpus-EDA](https://github.com/Exios66/Mailroom-Corpus-EDA) |
| Monorepo path | `packages/mailroom-corpus-eda` (virtual member) |
| Site | [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/) |
| Dataset | [`Lucius-Morningstar/mailroom-dataset`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset) |

## What it does

This repository profiles the 3,302-document corpus (five classes, 55 strata, 7.2x class imbalance) and is the one place the family publishes to the Hub from. The upload helpers that used to live in llm-entity-extraction were centralized here.

On this site (nested under [Data and corpora](../data-and-corpora.md) and this guide):

- [EDA visuals](../../how-it-fits-together/eda-visuals.md) — all 30 static PNGs from `reports/figures/`
- [Interactive charts](../../how-it-fits-together/eda-interactive.md) — live dashboard plus 18 Plotly HTMLs from `reports/figures_interactive/`

The GitBook pages embed the GitHub Pages copies at [exios66.github.io/Mailroom-Corpus-EDA](https://exios66.github.io/Mailroom-Corpus-EDA/).

`run_all.py` runs the analysis in phases:

| Phase | What | Output |
| :--- | :--- | :--- |
| P0 | Download and manifest validation | `data/parquet/` |
| P1 | Structural integrity and provenance audit | `reports/tables/integrity_report.json` |
| P2 | Composition: strata, imbalance, provenance | strata counts, imbalance metrics |
| P3 | Static figures and tables | `reports/figures/`, `reports/tables/` |
| P4 | Interactive Plotly figures | `reports/figures_interactive/` |
| P5 | Cast-safe JSONL and parquet staging | `data/staging/` |
| P6 | Correspondence intent coverage and provenance audit | `reports/SUMMARY_REPORT.json` |

## Quick start

```bash
git clone https://github.com/Exios66/Mailroom-Corpus-EDA.git
cd Mailroom-Corpus-EDA
pip install -r requirements.txt
python run_all.py                 # all phases
python run_all.py --phases P3 P4  # just the figures
```

## Hub helpers

| Module | Purpose |
| :--- | :--- |
| `src/mailroom_eda/hf_interface.py` | Hub client: upload, sha256 verification, repo management |
| `src/mailroom_eda/dataset_export.py` | Cast-safe metadata, JSONL line safety, parquet staging |
| `src/mailroom_eda/docclass_uploader.py` | Publishing with blind-label stripping and a ground-truth leak guard |
| `src/mailroom_eda/intent_backfill.py` | Correspondence intent hydration |
| `src/mailroom_eda/token_budget.py` | Token estimates and budget coverage |

## A note on the fork

[LLM-Mailroom-Services/Mailroom-Corpus](https://github.com/LLM-Mailroom-Services/Mailroom-Corpus) is an organization fork of this repository. See the [Repository index](../repo-index.md).

## Its documentation

- [README](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/README.md)
- [reports/SUMMARY_REPORT.md](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/reports/SUMMARY_REPORT.md) — narrative summary of findings
- [docs/dataset-cards/](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/docs/dataset-cards) — one card per source corpus, plus the [mailroom-dataset card](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/dataset-cards/mailroom-dataset.md)
- [docs/plans/](https://github.com/Exios66/Mailroom-Corpus-EDA/tree/main/docs/plans) — the v9.1 and v9.2 revision plans
- [CHANGELOG.md](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/CHANGELOG.md)
