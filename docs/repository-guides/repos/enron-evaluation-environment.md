# Enron-Evaluation-Environment

**Turns the CMU Enron email corpus into the pipeline's `correspondence` dataset.**

| | |
| :--- | :--- |
| Repository | [Exios66/Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment) |
| Monorepo path | `packages/Enron-Evaluation-Environment` (virtual member) |
| Site | [exios66.github.io/Enron-Evaluation-Environment](https://exios66.github.io/Enron-Evaluation-Environment/) |
| Feeds | the `correspondence` class |

## What it does

The repository runs exploratory analysis over all 517,390 parseable Enron emails and produces a stratified, duplicate-free sample for the pipeline (`data/enron/pipeline.jsonl`, about 400 documents), labeled with a 10-key correspondence subclass taxonomy.

Findings that shaped the dataset:

- **52.2% of message bodies are exact duplicates** (cc chains, sent-folder copies, mass mail), so the sampler de-duplicates by construction.
- **The subclass taxonomy is data-driven**, with false-positive guards (energy-market "demand" language is excluded; reply and forward chains cannot pass as memos).

The published Hub datasets (`enron-correspondence`, `enron-correspondence-dedup`) are what the canonical `mailroom-dataset` draws its correspondence rows from.

## Quick start

```bash
git clone https://github.com/Exios66/Enron-Evaluation-Environment.git
cd Enron-Evaluation-Environment
python scripts/acquire_enron.py        # ~423 MB download
python scripts/build_corpus_index.py   # parse to index.jsonl
python scripts/eda/explore_enron.py    # EDA reports and figures
python scripts/build_pipeline_dump.py  # stratified pipeline sample
pytest tests/ -v
```

## Handoff

The repo's position is "artifacts flow downstream; nothing flows back without a versioned handoff". llm-mailroom owns the taxonomy it labels against; llm-entity-extraction consumes the pipeline sample. The handoff contract is in [`reports/pipeline/README.md`](https://github.com/Exios66/Enron-Evaluation-Environment/blob/main/reports/pipeline/README.md).

## Its documentation

- [README](https://github.com/Exios66/Enron-Evaluation-Environment/blob/main/README.md)
- [reports/](https://github.com/Exios66/Enron-Evaluation-Environment/tree/main/reports) — EDA reports and the pipeline handoff
- [AGENTS.md](https://github.com/Exios66/Enron-Evaluation-Environment/blob/main/AGENTS.md)
