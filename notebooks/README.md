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

The **Hugging Face** surface is a separate notebook: `11_huggingface_corpora.ipynb`
walks the Lucius-Morningstar org (CUAD, LegalBench, Enron, DE-SynPUF) from a
committed Dataset Viewer snapshot. Set `MAILROOM_HF_LIVE=1` to refresh from
`https://datasets-server.huggingface.co` (optional `HF_TOKEN`).

## The suite (shipped)

`PLAN.md` is the plan of record; walkthroughs 00–13 now exist and
are guarded:

| # | notebook | what it teaches |
|---|----------|-----------------|
| 00 | `00_pipeline_anatomy` | static map: nodes, routers, lanes |
| 01 | `01_happy_path_run` | one clean run, step-by-step state deltas ★ |
| 02 | `02_routing_dynamics` | confidence bands → five different paths for one document |
| 03 | `03_review_lanes` | Lane A reviewer + Lane B judge/arbiter, incl. the bounded retry firing end-to-end (fixed under KANBAN-098) |
| 04 | `04_human_in_the_loop` | park → inspect → approve/reject, real checkpointer threads |
| 05 | `05_failure_recovery` | transient-error ladder vs confidence budget (L-13) |
| 06 | `06_outputs_and_audit` | manifests, catalog, bins, audit chain — who eats what |
| 07 | `07_multi_document_matters` | several documents, one `matter_id`, catalog rollup |
| 08 | `08_observability_traces` | the Langfuse trace contract, offline (+ marker-gated live cell) |
| 09 | `09_all_specialists` | one happy-path run per document class — all 7 specialists |
| 10 | `10_edge_cases` | unknown type, missing CUAD subtype, $0 amounts, schema-invalid extract, Boss conflict |
| 11 | `11_huggingface_corpora` | Lucius-Morningstar Hub datasets (offline Dataset Viewer snapshot + live opt-in) |
| 12 | `12_legalbench` | LegalBench eval suite (mock on a mini CUAD fixture; Hub pack is a stub) |
| 13 | `13_vision_ingestion` | additive page-image render path (PyMuPDF data-URIs, no LLM call) |

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
