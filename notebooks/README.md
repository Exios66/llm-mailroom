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

## The suite plan

`PLAN.md` is the plan of record for the full notebook suite: nine walkthroughs
(`00` pipeline anatomy → `08` observability traces) covering the graph map,
an example run through the agents (outputs + the role of each agent), routing
dynamics, the review/judge/arbiter lanes, human-in-the-loop resume, failure
recovery, outputs/audit, matter grouping, and the trace contract — all driven
through one shared `pipeline_lab.py` bench on the REAL graph with the test
suite's network-free mock seam. Implementation proceeds one notebook per
commit per the plan's build order.
