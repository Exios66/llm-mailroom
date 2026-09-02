# Operational Scripts

This directory contains all operational and evaluation scripts for the LLM-Mailroom pipeline. Each script is designed to be run standalone from the project root.

## Pipeline Operations

| Script | Purpose |
|--------|---------|
| `run_pilot.py` | Main committed-sample pilot. Runs the full pipeline over the sample set with mock (`--mock`) or real (`--real`) LLM. Supports baseline diffing (`--baseline`) and ground-truth score ingestion (`--scores`). |
| `run_hf_pilot.py` | Hugging Face corpus pilot. Default corpus is `Lucius-Morningstar/mailroom-corpus` **schema v5** (1,210 docs: CUAD / MAUD / S-1 / Enron sample / CMS). Class × subclass **examples** come from `Lucius-Morningstar/docclass-pilot` (48 strata) — `--examples` / `--dataset examples`. Other Lucius-Morningstar pipeline corpora (`enron-correspondence-dedup` ~247k, `cms-desynpuf-insurance-claims`, `mailroom-cuad-contracts[-full]`) via `--dataset <slug>`. `--check` / `--real --per-class N` / `--per-subclass N`. Writes `data/hf_pilot/<stamp>/report.json` with `session_id=pilot-hf-<stamp>`. |
| `run_quality_judges.py` | Offline LLM-as-a-Judge evaluation over a pilot run. Measures classification correctness, extraction completeness, and extraction correctness. Supports `--mock` for deterministic fake judges. |
| `run_agent_eval.py` | Per-agent isolation eval (no 13-node graph). `--list` / `--agent <name>|all` / `--mock` / `--real` / `--n` / `--self-check`. `--real` is gated by `is_real_sample` the same way `run_pilot.py` is. |
| `run_vision_sweep.py` | Vision vs. text tradeoff benchmarking. Runs the same documents with text-only, vision-10-pages, and vision-all-pages modes. Outputs comparison metrics. |
| `write_pilot_report.py` | Renders tracked markdown + JSON pilot report from collected run data (default: `docs/reports/pilots/pilot-vision-tradeoff.md`). |

## Data Preparation

| Script | Purpose |
|--------|---------|
| `prepare_samples.py` | Builds the pilot PDF set in `data/samples/` from committed sources and `docs/examples/sources/`. |
| `fetch_external_samples.py` | Downloads LegalBench MAUD, Atticus CUAD, and Pile of Law samples into `docs/examples/external/`. Idempotent. |
| `fetch_full_cuad.py` | Downloads the **full CUAD corpus** (510 annotated contracts, 200 txt, 199 PDFs) into `data/cuad/` and writes the subtype-distribution EDA. Idempotent — resumes partial downloads. |
| `validate_pipeline.py` | CLI smoke test of the full pipeline (mock LLM) over sample files; supports `--count`, `--doc-type`, and direct file arguments. Exit code 0 only if every file reaches archive. |

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
| `scripts/cutover.py` | Per-agent provider/model switching utility. `--list` shows current assignments, `--recommend` suggests cutover order, `--validate --agent <name>` runs tests against the proposed model, `--agent <name> --provider <p> --model <m>` updates `taxonomy.yaml`. |
|

## Utility

| Script | Purpose |
|--------|---------|
| `compare_runs.py` | Compares two pilot run reports and outputs a diff of stage changes, confidence shifts, and extraction differences. |
| `recover_processing.py` | Audit L-1/A-18: reconciles `processing/<worker_id>/` claims orphaned by crashed processes — re-queues to the inbox when no terminal manifest exists, retires to `failed/` when one does. `--apply` to move (default dry-run), `--stale-minutes N`, `--move-all-to-failed`. |
| `verify_audit_chains.py` | Recompute every per-doc SHA-256 audit chain; nonzero exit on breaks. |
| `analyze_audit_db.py` | Parse/summarize the full local audit DB (event/actor histograms, review events, chain health, optional catalog join). `--json`, `--no-verify`, `--join-catalog`. |
| `export_warehouse.py` | Export terminal (`archived`/`failed`) catalog rows + matching audit chains to daily Parquet under `data/warehouse/` (`documents_YYYY-MM-DD.parquet`, `audit_YYYY-MM-DD.parquet`, `manifest.json`). `--full`, `--date`, `--doc-id`, `--since`. |
| `new_report.py` | Scaffolds a new evaluation write-up / audit / report under `docs/reports/<kind>/` (`audits` \| `pilots` \| `evaluations`) with a dated kebab-case filename and a standard header. Use this for any future report — never drop reports in the repo root. Supports `--date` and `--dry-run`. |
| `publish_space.py` | Publish the producer Docker Space (`--check` is network-free). Default Hub id `<whoami>/mailroom-producer`. |
| `probe_hosted_spaces.py` | Probe the live `Lucius-Morningstar` Observatory + producer pair (`--offline` prints pins only). |
| `calibrate_field_scoring.py` | Issue #4 calibration step: builds a labeled field sample from `docs/examples/samples/manifest.csv` ground truth (exact/format variants = correct; controlled perturbations = incorrect) and reports per-field-type score separation + calibrated `field_scoring.type_bands` cutoffs. Supports `--band LOW HIGH` to evaluate a candidate band and `--json` for machine-readable output. |

## Common Patterns

### Running with Mock LLM (No API Key)
```bash
PYTHONPATH=src python src/scripts/run_pilot.py --mock
PYTHONPATH=src python src/scripts/run_quality_judges.py --mock
PYTHONPATH=src python src/scripts/run_vision_sweep.py --mock
```

### Running with Real LLM (Requires OPENROUTER_API_KEY)
```bash
# In .env: OPENROUTER_API_KEY=sk-or-v1-...
PYTHONPATH=src python src/scripts/run_pilot.py --real
PYTHONPATH=src python src/scripts/run_quality_judges.py --real
PYTHONPATH=src python src/scripts/run_vision_sweep.py --real
```

### Dry Runs
Most sync scripts support `--dry-run` to preview changes without writing:
```bash
PYTHONPATH=src python src/scripts/sync_prompts.py --dry-run
PYTHONPATH=src python src/scripts/sync_evaluators.py --dry-run
PYTHONPATH=src python src/scripts/sync_dataset.py --dry-run
PYTHONPATH=src python src/scripts/sync_models.py --dry-run
```

## Environment Variables

Scripts respect the following from `.env`:
- `OPENROUTER_API_KEY` — required for `--real` runs
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` — required for Langfuse sync scripts
- `MAILROOM_BASE_DIR` — defaults to `./data` (pilot runs use temp dirs)
- `OBSERVABILITY_PROVIDER` — `auto|langfuse|braintrust|none`

## Testing

All scripts are designed to be run manually. There are no automated tests for scripts themselves, but they are exercised during:
- `pytesttest_pipeline_e2e.py` (via `run_pilot.py --mock`)
- `pytesttest_quality_judges.py` (via `run_quality_judges.py --mock`)
- `pytesttest_vision.py` (via `run_vision_sweep.py --mock`)

## Notes

- Scripts that write to Langfuse (`sync_*.py`) are **idempotent** — safe to re-run.
- Pilot runs create deterministic trace IDs seeded from filenames; re-runs keep the first run's environment/tags.
- Real runs (`--real`) process **only the 21 actual committed legal documents** (9 CUAD/Atticus contracts + 6 LegalBench + 6 Pile of Law). The 9 synthetic samples are **mock-only** and will be refused by `--real` to avoid spending tokens on fake documents.