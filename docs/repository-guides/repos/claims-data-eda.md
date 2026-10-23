# claims-data-eda

**Turns CMS Medicare synthetic claims into plain-text EOB documents for the `insurance_claim` class.**

| | |
| :--- | :--- |
| Repository | [Exios66/claims-data-eda](https://github.com/Exios66/claims-data-eda) |
| Hub status | External corpus feeder in the DMR-093 target |
| Hub preservation | [`feature/claims-data-eda: packages/claims-data-eda`](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/claims-data-eda/packages/claims-data-eda) |
| Site | [exios66.github.io/claims-data-eda](https://exios66.github.io/claims-data-eda/) |
| Feeds | the `insurance_claim` class |

[DMR-093](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/issues/165) removes this feeder from the active hub workspace in a separate pruning PR after [#162](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/pull/162). Until that PR lands, hub `main` retains it in the seven-member DMR-079 layout. The linked preservation branch is published at hub commit `59c879d9`; final PR162 must retain its exact feeder package tree before pruning. This standalone repository, its data provenance, site, and handoff remain active; author and test source changes here first.

## What it does

The repository analyzes the full CMS 2008 to 2010 DE-SynPUF Sample 1 (about 11.15 million claim events across inpatient, outpatient, carrier and prescription files, linked to 116,352 beneficiaries) and renders each sampled event as an Explanation of Benefits document. Ground-truth fields line up with the pipeline's `InsuranceClaimExtraction` schema.

Things worth knowing:

- **Costs are heavy-tailed**, so the sampler buckets on log-cost and keeps at least 15% high-cost claims per type.
- **Three of the source archives are no longer hosted by CMS** and were recovered from Internet Archive captures. Every ZIP is sha256-recorded in `data/raw/MANIFEST.json`.
- **CMS ground truth is homogeneous** (claims are all approved), so `determination_consistency` is treated as a quality KPI rather than a discriminating benchmark. The canonical dataset adds other insurance sources (GNOTHEIA, BDR, INSURBIAS) for variety.

## Quick start

```bash
git clone https://github.com/Exios66/claims-data-eda.git
cd claims-data-eda
python scripts/acquire_synpuf.py              # ~356 MB, resumable
python scripts/build_corpus_index.py          # unified index
python scripts/eda/explore_cms.py             # EDA reports
python scripts/build_pipeline_dump.py --n 400 # stratified pipeline sample
pytest tests/ -v
```

## Its documentation

- [README](https://github.com/Exios66/claims-data-eda/blob/main/README.md)
- [reports/pipeline/README.md](https://github.com/Exios66/claims-data-eda/blob/main/reports/pipeline/README.md) — handoff contract
- [reports/](https://github.com/Exios66/claims-data-eda/tree/main/reports) — EDA reports
- [AGENTS.md](https://github.com/Exios66/claims-data-eda/blob/main/AGENTS.md)
