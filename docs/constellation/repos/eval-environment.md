# eval-environment

**Per-node performance evaluation, pilots and calibration for every LLM node in the pipeline.**

| | |
| :--- | :--- |
| Repository | [LLM-Mailroom-Services/eval-environment](https://github.com/LLM-Mailroom-Services/eval-environment) (package `mailroom-evals`) |
| Viewer | [eval-environment.vercel.app](https://eval-environment.vercel.app) |
| Pipeline under test | llm-mailroom, resolved as an editable path source |
| Dataset | `mailroom-dataset` pinned to `ed7576b6` |

## What it does

eval-environment holds one task registry (31 tasks) covering every LLM-based node: intake, the sorter, the five extraction specialists, judge and arbiter, boss escalation, the archivist, and the full chained pipeline. Each task can call the real graph node with a faithful `DocumentState`, or call the agent class directly.

| Family | Purpose |
| :--- | :--- |
| `eval:<node>` | Measure accuracy, quality, latency, tokens and cost for one node |
| `pilot:<node or chain>` | Cheap wiring check on a stratified micro-slice before a full sweep |
| `calibration:<node>` | Reliability tables, ECE and threshold sweeps; recommends thresholds but never changes them |

Every run gets a `run_id`, appends one line to `reports/experiment_log.jsonl`, and writes a self-contained folder under `data/experiments/<run_id>/`.

## Quick start

```bash
uv sync --extra dev
uv run python scripts/run_evals.py --list
uv run python scripts/run_evals.py --task all --mock --n 2         # hermetic smoke test
uv run python scripts/run_evals.py --task eval:insurance_claims --real \
    --subset class:insurance_claim --sample 25 --seed 42
uv run python scripts/render_experiment_log.py
```

Always run `--mock` first. Real runs need `OPENROUTER_API_KEY` (or the Vercel AI Gateway settings described in the README).

## Rules worth knowing

- **Never traces to Langfuse.** Traces go to Braintrust when a key is set, otherwise local Arize Phoenix, so eval runs never mix with production traces.
- **Subset grammar.** `full`, `train`, `test`, `class:<x>`, `subclass:<x>`, `fixtures`, `bundles`, `streams`, `pilot`, `cuad`, `enron`, `claims`.
- **Frozen prompt lineage.** `mailroom-evals-v1` is the official prompt version 1, injected with provenance (sha256 and pipeline commit per run) and drift detection. It seeds the GEPA mutation loop.
- **Full scoring is post hoc.** Sinks carry tracing plus essential scores; the full suite (recompute, local LLM judge, A/B with bootstrap CIs) runs later from the experiment log.

## Its documentation

- [README](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/README.md)
- [docs/tasks.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/tasks.md) — the task catalog
- [docs/scoring.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/scoring.md)
- [docs/calibration.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/calibration.md)
- [docs/experiment-log.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/experiment-log.md)
- [docs/prompt-lineage.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/prompt-lineage.md)
- [docs/pipeline-checkout.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/pipeline-checkout.md) — wiring the pipeline under test
- [docs/openrouter-braintrust-runbook.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/openrouter-braintrust-runbook.md)
- [docs/intake-classifier-proposal.md](https://github.com/LLM-Mailroom-Services/eval-environment/blob/main/docs/intake-classifier-proposal.md)
