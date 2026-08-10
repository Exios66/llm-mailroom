# Operational Scripts

This directory contains all operational and evaluation scripts for the LLM-Mailroom pipeline. Each script is designed to be run standalone from the project root.

## Pipeline Operations

| Script | Purpose |
|--------|---------|
| `run_pilot.py` | Main pilot testing entrypoint. Runs the full pipeline over the sample set with mock (`--mock`) or real (`--real`) LLM. Supports baseline diffing (`--baseline`) and ground-truth score ingestion (`--scores`). |
| `run_quality_judges.py` | Offline LLM-as-a-Judge evaluation over a pilot run. Measures classification correctness, extraction completeness, and extraction correctness. Supports `--mock` for deterministic fake judges. |
| `run_vision_sweep.py` | Vision vs. text tradeoff benchmarking. Runs the same documents with text-only, vision-10-pages, and vision-all-pages modes. Outputs comparison metrics. |
| `write_pilot_report.py` | Renders tracked markdown + JSON pilot report from collected run data (default: `reports/pilot-vision-tradeoff.md`). |

## Data Preparation

| Script | Purpose |
|--------|---------|
| `prepare_samples.py` | Builds the pilot PDF set in `data/samples/` from committed sources and `examples/sources/`. |
| `fetch_external_samples.py` | Downloads LegalBench MAUD, Atticus CUAD, and Pile of Law samples into `examples/external/`. Idempotent. |

## Langfuse Synchronization

| Script | Purpose |
|--------|---------|
| `sync_prompts.py` | Pushes agent prompt templates (`llm/prompts.py:prompt_templates()`) to Langfuse Prompt Management. Idempotent — only creates new versions on change. Supports `--dry-run` and `--agent <name>`. |
| `sync_evaluators.py` | Creates/updates the two LLM-as-a-Judge evaluators (`mailroom-pipeline-judge` CORRECT/PARTIAL/MISS, `mailroom-pipeline-quality` 0.0-1.0) and their observation rules targeting the `pipeline-result` generation. Prunes stale mailroom evaluators/rules. Supports `--dry-run` and `--disable`. |
| `sync_dataset.py` | Mirrors pilot samples (PDF text + manifest metadata + ground truth `expected_fields`) into Langfuse datasets: `mailroom-pilot` + per-corpus `mailroom-pilot-{legalbench,atticus,pileoflaw}`. Supports `--include <class>`. |
| `sync_langfuse_logs.py` | Pulls traces (with observations + scores) from Langfuse into `data/langfuse_logs/<run>/` for offline analysis. Supports `--since`, `--limit`, `--trace-id`. |
| `sync_models.py` | Syncs model pricing from `config/taxonomy.yaml:cost_models` to Langfuse Model Registry. Supports `--force` to clear 24h negative cache. |
| `sync_dashboards.py` | Syncs the two Mailroom health dashboards to Langfuse (idempotent, definitions in version control). |

## Model Management

| Script | Purpose |
|--------|---------|
| `cutover.py` | Per-agent provider/model switching utility. `--list` shows current assignments, `--recommend` suggests cutover order, `--validate --agent <name>` runs tests against the proposed model, `--agent <name> --provider <p> --model <m>` updates `taxonomy.yaml`. |
|

## Utility

| Script | Purpose |
|--------|---------|
| `compare_runs.py` | Compares two pilot run reports and outputs a diff of stage changes, confidence shifts, and extraction differences. |

## Common Patterns

### Running with Mock LLM (No API Key)
```bash
python scripts/run_pilot.py --mock
python scripts/run_quality_judges.py --mock
python scripts/run_vision_sweep.py --mock
```

### Running with Real LLM (Requires OPENROUTER_API_KEY)
```bash
# In .env: OPENROUTER_API_KEY=sk-or-v1-...
python scripts/run_pilot.py --real
python scripts/run_quality_judges.py --real
python scripts/run_vision_sweep.py --real
```

### Dry Runs
Most sync scripts support `--dry-run` to preview changes without writing:
```bash
python scripts/sync_prompts.py --dry-run
python scripts/sync_evaluators.py --dry-run
python scripts/sync_dataset.py --dry-run
python scripts/sync_models.py --dry-run
```

## Environment Variables

Scripts respect the following from `.env`:
- `OPENROUTER_API_KEY` — required for `--real` runs
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` — required for Langfuse sync scripts
- `MAILROOM_BASE_DIR` — defaults to `./data` (pilot runs use temp dirs)
- `OBSERVABILITY_PROVIDER` — `auto|langfuse|braintrust|none`

## Testing

All scripts are designed to be run manually. There are no automated tests for scripts themselves, but they are exercised during:
- `pytest tests/test_pipeline_e2e.py` (via `run_pilot.py --mock`)
- `pytest tests/test_quality_judges.py` (via `run_quality_judges.py --mock`)
- `pytest tests/test_vision.py` (via `run_vision_sweep.py --mock`)

## Notes

- Scripts that write to Langfuse (`sync_*.py`) are **idempotent** — safe to re-run.
- Pilot runs create deterministic trace IDs seeded from filenames; re-runs keep the first run's environment/tags.
- Real runs (`--real`) process **only the 21 actual committed legal documents** (9 CUAD/Atticus contracts + 6 LegalBench + 6 Pile of Law). The 9 synthetic samples are **mock-only** and will be refused by `--real` to avoid spending tokens on fake documents.