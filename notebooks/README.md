# Notebooks

Jupyter notebooks for the mailroom. Pattern follows
[rossumai/docile](https://github.com/rossumai/docile) (`tools/dataset_browser.ipynb`):
**thin notebook + reusable tool module** — the module next to each notebook does
the actual work and is importable/testable without Jupyter.

## dataset_browser

Browse the pilot sample set (ground truth from
`docs/examples/samples/manifest.csv`) joined with the pipeline's observed state
(catalog `data/mailroom.db`, opened **read-only**) per sample.

```bash
# one-time sample materialization (writes data/samples/, gitignored)
PYTHONPATH=src python src/scripts/prepare_samples.py

# interactive widget (optional)
pip install -e ".[notebooks]"

# launch
jupyter lab notebooks/dataset_browser.ipynb   # or jupyter notebook
```

Without the extra, the browser still runs in plain-text mode — every function is
importable with only the core install. No network access, no LLM calls.

## The suite (shipped)

`PLAN.md` is the plan of record; all nine planned walkthroughs now exist and
are guarded:

| # | notebook | what it teaches |
|---|----------|-----------------|
| 00 | `00_pipeline_anatomy` | static map: nodes, routers, lanes |
| 01 | `01_happy_path_run` | one clean run, step-by-step state deltas ★ |
| 02 | `02_routing_dynamics` | confidence bands → five different paths for one document |
| 03 | `03_review_lanes` | Lane A reviewer + Lane B judge/arbiter, incl. a demonstrated wiring trap |
| 04 | `04_human_in_the_loop` | park → inspect → approve/reject, real checkpointer threads |
| 05 | `05_failure_recovery` | transient-error ladder vs confidence budget (L-13) |
| 06 | `06_outputs_and_audit` | manifests, catalog, bins, audit chain — who eats what |
| 07 | `07_multi_document_matters` | several documents, one `matter_id`, catalog rollup |
| 08 | `08_observability_traces` | the Langfuse trace contract, offline (+ marker-gated live cell) |

★ = Jack's headline ask.

**Honesty contract:** every run in every notebook is the REAL pipeline
(`graph.build_graph.run_pipeline`) on the REAL machinery — checkpointer,
routers, bins, SQLite — with the test suite's network-free mock seam standing
in for the LLMs. Mocked intelligence, real machinery. Each notebook says so
in its honesty-label cell.

**Guards:** `src/tests/test_notebook_suite.py` enforces the four PLAN duties —
existence/shape, headless re-execution from both PLAN cwds (repo root and
notebooks/) with stored outputs regenerating error-free, `pipeline_lab` unit
pins against the routing literals, and secret/network AST scans (notebook
08's opt-in cell excepted by the `NB-OPT-IN-NETWORK` marker).

```bash
# re-execute any notebook headlessly (as the guard does)
pip install -e ".[notebooks]"
jupyter execute notebooks/03_review_lanes.ipynb
```
