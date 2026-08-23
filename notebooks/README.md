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
