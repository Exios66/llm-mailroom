# Examples

This directory contains example data and reference materials for the LLM-Mailroom pipeline.

## Contents

### `samples/`

The **pilot sample set** — 25 curated legal PDFs spanning all six live document classes in `config/taxonomy.yaml`, used to pilot-test the pipeline and evaluate procedural changes.

| Class | Count | Source |
|-------|-------|--------|
| `contract` | 9 | 3 original CUAD SEC-exhibit PDFs + 6 CUAD/Atticus PDFs (CC-BY-4.0) |
| `merger_agreement` | 6 | LegalBench MAUD v1 merger agreements (CC-BY-4.0) |
| `compliance_filing` | 2 | Synthetic 10-K excerpt (large) + state filing |
| `corporate_record` | 2 | Bylaws + board resolution |
| `correspondence` | 2 | Demand letter + internal memo |
| `insurance_claim` | 3 | Synthetic FNOL / coverage letters (approved / denied / partial) |
| `ambiguous` | 1 | Multi-topic memo → expects human review |

**Key files:**
- `manifest.csv` — ground truth per sample (doc_class, stage, size, source, license, dataset tag)
- `ATTRIBUTION.md` — per-source license notes
- `contract/*.pdf` — real CUAD PDFs (committed)
- `external/legalbench/*.txt` — MAUD merger agreement texts (committed)
- `external/pileoflaw/*.txt` — court opinion texts (on disk; retired from live pilot)
- `sources/<class>/*.txt` — original text used to synthesize the rest

See [`samples/README.md`](samples/README.md) for full details on running pilot tests.

### `external/`

External data sources fetched by `scripts/fetch_external_samples.py`:
- **LegalBench MAUD** — 6 merger agreement texts (CC-BY-4.0)
- **Pile of Law** — 6 U.S. court opinion texts (public domain; retired from the live manifest)

### `sources/`

Original text files used to synthesize the non-contract document classes (compliance, corporate_record, correspondence, ambiguous). Due-diligence sources remain on disk but are not in the live pilot.

## Quick Start

```bash
# Fetch external samples (one-time, idempotent)
PYTHONPATH=src python src/scripts/fetch_external_samples.py

# Build the sample PDFs into data/samples/ (gitignored)
PYTHONPATH=src python src/scripts/prepare_samples.py

# Run pilot test with mock LLM (no API key needed)
PYTHONPATH=src python src/scripts/run_pilot.py --mock

# Run pilot test with real LLM (needs OPENROUTER_API_KEY)
PYTHONPATH=src python src/scripts/run_pilot.py --real

# Sync evaluation datasets to Langfuse
PYTHONPATH=src python src/scripts/sync_dataset.py
```

## Licensing

See [`samples/ATTRIBUTION.md`](samples/ATTRIBUTION.md). CUAD contracts and LegalBench/MAUD merger agreements are CC BY 4.0 (The Atticus Project); Pile of Law samples are public-domain U.S. government works; all other sample text is original to this repo.