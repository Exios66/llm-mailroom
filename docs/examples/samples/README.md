# Pilot Sample Set

A curated set of legal PDFs used to **pilot-test the pipeline and evaluate
procedural changes** (accuracy + efficiency). 22 documents spanning five of
the six live `config/taxonomy.yaml` doc classes (`insurance_claim` is live
but not in this set), plus one deliberately ambiguous memo that drives the
retry → human-review path.

| Class | Count | Source |
|---|---|---|
| `contract` | 9 | 3 original CUAD SEC-exhibit PDFs + 6 CUAD/Atticus PDFs (CC-BY-4.0) |
| `merger_agreement` | 6 | LegalBench MAUD v1 merger agreements (CC-BY-4.0) |
| `compliance_filing` | 2 | Synthetic 10-K excerpt (large) + state filing |
| `corporate_record` | 2 | Bylaws + board resolution |
| `correspondence` | 2 | Demand letter + internal memo |
| `ambiguous` | 1 | Multi-topic memo → expects human review |

`due_diligence` and `court_opinion` were retired from the live taxonomy.
Their source files remain on disk (synthetic DD texts + Pile of Law opinions)
but are **not** in `manifest.csv` and are not processed by `--real` or `--mock`
pilot runs.

Each sample carries a `dataset` tag in `manifest.csv` so a source corpus can be
run or synced on its own:

| dataset | samples | origin |
|---|---|---|
| `original` | 10 | committed CUAD PDFs + synthetic text (original) |
| `legalbench` | 6 | MAUD v1 merger agreements — the full texts behind LegalBench's `maud_*` tasks (Zenodo 10.5281/zenodo.7500064) |
| `atticus` | 6 | CUAD v1 contract PDFs from `theatticusproject/cuad` |

Size tiers (`small` / `medium` / `large`) are recorded in `manifest.csv` so you
can benchmark the effect of document length on latency and LLM cost.

## Layout

```
examples/
  samples/
    manifest.csv          # ground truth per sample (class, stage, size, source, license, dataset)
    ATTRIBUTION.md        # per-source license notes
    contract/*.pdf        # real CUAD PDFs (committed; 3 original + 6 atticus_*)
  external/
    legalbench/*.txt      # MAUD merger agreement texts (committed)
    pileoflaw/*.txt       # court opinion texts (on disk; retired from live pilot)
  sources/<class>/*.txt   # original text used to synthesize the rest
scripts/
  fetch_external_samples.py  # downloads the 18 external samples (idempotent)
  prepare_samples.py      # builds data/samples/ (copies CUAD + renders sources)
  run_pilot.py            # feeds samples through the pipeline and evaluates
  sync_dataset.py         # syncs per-source datasets to Langfuse
```

## How to run

```bash
# 0. (one-time) fetch the 18 external samples from LegalBench / Atticus / Pile of Law
PYTHONPATH=src python src/scripts/fetch_external_samples.py

# 1. Generate/copy the sample PDFs into data/samples/ (gitignored)
PYTHONPATH=src python src/scripts/prepare_samples.py

# 2. Pilot-test the pipeline (deterministic mock LLM, no API key needed)
PYTHONPATH=src python src/scripts/run_pilot.py --mock

# 3. Or run for real (needs OPENROUTER_API_KEY in .env)
PYTHONPATH=src python src/scripts/run_pilot.py --real

# Real runs process ONLY the actual committed legal documents: the 9
# Atticus/CUAD contract & agreement PDFs (contract_01..03, atticus_01..06)
# plus the 6 LegalBench MAUD samples (15 real samples). The repo-written
# synthetic .txt-derived PDFs (compliance / corporate / correspondence /
# ambiguous, 7 samples) are **mock-only** — --real refuses them so no real
# LLM/eval tokens or live traces are ever spent on fake documents. Mock runs
# keep the full live-manifest set (22). Pile of Law court opinions stay on
# disk but are not in the live manifest.

# 4. Run a single source corpus
PYTHONPATH=src python src/scripts/run_pilot.py --mock --source legalbench

# 5. Compare a procedural change against a saved baseline
PYTHONPATH=src python src/scripts/run_pilot.py --mock --baseline data/pilot_report_baseline.json

# 6. Sync the evaluation datasets to Langfuse (one dataset per source)
PYTHONPATH=src python src/scripts/sync_dataset.py --dry-run     # preview
PYTHONPATH=src python src/scripts/sync_dataset.py               # mailroom-pilot[-legalbench|-atticus]
```

## What to expect

- High-confidence happy paths reach `archived` with the expected `doc_class`.
- `ambiguous_01_mixed_memo.pdf` is expected to land in `review` (low confidence
  → retry → human review), exercising the conditional routing.
- `contract_03` (52 pages), the `legalbench_04..06` merger agreements, and
  `compliance_01` (10-K excerpt) are the large-document efficiency cases; they
  exercise the classify-truncation path (`doc_text[:12000]`) and longer
  transcription/extraction times.

## Licensing

See `ATTRIBUTION.md`. CUAD contracts and LegalBench/MAUD merger agreements are
CC BY 4.0 (The Atticus Project); Pile of Law samples are public-domain U.S.
government works (the Pile of Law compilation itself, CC BY-NC-SA 4.0, is not
committed); all other sample text is original to this repo.
