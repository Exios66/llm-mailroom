# Pilot Sample Set

A curated set of legal PDFs used to **pilot-test the pipeline and evaluate
procedural changes** (accuracy + efficiency). 12 documents spanning all five
`config/taxonomy.yaml` doc classes, plus one deliberately ambiguous memo that
drives the retry → human-review path.

| Class | Count | Source |
|---|---|---|
| `contract` | 3 | Real SEC-exhibit contracts from CUAD (CC-BY-4.0) |
| `compliance_filing` | 2 | Synthetic 10-K excerpt (large) + state filing |
| `corporate_record` | 2 | Bylaws + board resolution |
| `correspondence` | 2 | Demand letter + internal memo |
| `due_diligence` | 2 | DD report + checklist |
| `ambiguous` | 1 | Multi-topic memo → expects human review |

Size tiers (`small` / `medium` / `large`) are recorded in `manifest.csv` so you
can benchmark the effect of document length on latency and LLM cost.

## Layout

```
examples/
  samples/
    manifest.csv          # ground truth per sample (class, stage, size, source, license)
    ATTRIBUTION.md        # per-source license notes
    contract/*.pdf        # real CUAD PDFs (committed)
  sources/<class>/*.txt   # original text used to synthesize the rest
scripts/
  prepare_samples.py      # builds data/samples/ (copies CUAD + renders sources)
  run_pilot.py            # feeds samples through the pipeline and evaluates
```

## How to run

```bash
# 1. Generate/copy the sample PDFs into data/samples/ (gitignored)
python scripts/prepare_samples.py

# 2. Pilot-test the pipeline (deterministic mock LLM, no API key needed)
python scripts/run_pilot.py --mock

# 3. Or run for real (needs OPENROUTER_API_KEY in .env)
python scripts/run_pilot.py --real

# 4. Compare a procedural change against a saved baseline
python scripts/run_pilot.py --mock --baseline data/pilot_report_baseline.json
```

## What to expect

- High-confidence happy paths reach `archived` with the expected `doc_class`.
- `ambiguous_01_mixed_memo.pdf` is expected to land in `review` (low confidence
  → retry → human review), exercising the conditional routing.
- `contract_03` (52 pages) and `compliance_01` (10-K excerpt) are the
  large-document efficiency cases; they exercise the classify-truncation path
  (`doc_text[:12000]`) and longer transcription/extraction times.

## Licensing

See `ATTRIBUTION.md`. The CUAD contracts are CC BY 4.0 (The Atticus Project);
all other sample text is original to this repo.
