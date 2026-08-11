# LegalBench evaluation suite

A self-contained evaluation submodule that evaluates models through the
**LegalBench task families**, using corpora already mirrored in this repo
(no network needed):

| Task | Family | Corpus | Units | Scoring |
|---|---|---|---|---|
| `contract_qa` | **binary answer** (yes/no) | full CUAD annotations — `data/cuad/CUAD_v1.json`: 510 contracts × 41 clause categories = **20,910 questions** with evidence spans | questions | accuracy, macro per-category accuracy, yes-class F1, ECE calibration |
| `family_classification` | **multiclass classification** | `data/cuad/contracts/` — 200 plain-text contracts labeled with one of the 25 CUAD contract families (+ `other`) via the vendored sorter taxonomy | documents | strict + equiv accuracy, macro-F1, calibration |

It is a **second lens** on model quality: it runs alongside — never inside —
the pipeline's own eval tasks, reuses the vendored agent machinery (retry
contract, usage accounting, input truncation), traces every run to Langfuse,
and appends each completed run to the **experiment log + experiment-log
site** automatically.

## Quick start

```bash
# list tasks
python -m legalbench.cli --list-tasks

# real run (needs OPENROUTER_API_KEY; optional Langfuse keys for tracing)
python -m legalbench.cli --task contract_qa --n 30 --model qwen/qwen3.7-flash

# deterministic mock run — no API key, no network, NOT real results
python -m legalbench.cli --task family_classification --n 20 --mock

# skip Langfuse and/or the experiment log
python -m legalbench.cli --task contract_qa --n 10 --no-trace --no-log
```

## What happens on completion

1. **Scoring** — deterministic, local, never LLM-graded: every question's
   prediction is compared against the corpus ground truth.
2. **Langfuse** — one trace per run (`legalbench-<task>`, deterministic
   seed = run id), one child span per question, and the run-level scores
   attached via the registered `legalbench_*` score configs
   (`observability/scores.py`). All no-op when observability is disabled.
3. **Experiment log** — one JSON record appended to the shared append-only
   log (default: the sibling repo's `reports/experiment_log.jsonl` when
   `../llm-entity-extraction` exists, else `src/legalbench/reports/`). The
   record uses the upstream schema, so it renders in the markdown log and
   the experiment-log site like every other run.
4. **Log + site regeneration** — the upstream `render_experiment_log.py`
   and `build_site.py` rebuild the markdown log and the site data, and the
   synced copy at `docs/reports/experiments/experiment_log.md` is refreshed
   (SYNCED-DOCUMENT header, same convention as before).

Committing/pushing the sibling repo (and the docs sync here) are explicit
steps — the runner only writes files.

## Paths & knobs

| Knob | Default |
|---|---|
| `LEGALBENCH_EXPERIMENT_LOG` | sibling `reports/experiment_log.jsonl`, else `src/legalbench/reports/experiment_log.jsonl` |
| `LEGALBENCH_SIBLING_REPO` | `<repo>/../llm-entity-extraction` |

The sibling rebuild only runs when the log path is **inside** the sibling
repo — a throwaway/partial log never clobbers the real site data.

## Adding a task

1. Add a loader to `src/legalbench/data.py` (corpus → sampled rows with
   `expected` labels).
2. Add a versioned prompt to `src/legalbench/prompts.py` (version = experiment
   identity; never mutate after a logged run).
3. Register the task in `src/legalbench/tasks.py` (loader, call shape, extractor,
   scorer, classes).
4. Add the task's headline/breakdown handlers to the upstream
   `scripts/site/build_site.py` so the site renders it.
5. Add tests in `tests/test_legalbench.py` (synthetic corpora, network-free).

## Honesty notes

- `--mock` runs use a deterministic fake model (stable hashes) and are
  labelled `mock/mock-legalbench` in the log — baseline/repro only, never
  real results.
- Accuracy is only comparable **within the same sample** (same task, seed,
  n) — same rule as the upstream eval loop.
- Corpora are the full local CUAD mirror; re-run `src/scripts/fetch_full_cuad.py`
  to refresh them (needs network once).
