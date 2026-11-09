# Quick start

## What is available now

This folder currently contains the implementation proposal and setup guides. Use the existing commands below to inspect and test the current pipeline. The new React workbench and `mailroom-diagnostics` command are planned; they are not installed or runnable yet.

Run commands from the **repository root**, not from this tools folder. Use Python 3.11+; Python 3.12 is the environment verified for this setup. Do not use `uv sync` or `uv pip install -e .`: the root uv source entry points at a separate workspace.

### 1. Install the declared dependencies

If a repository-local `.venv` already exists, use it and skip the creation command. Otherwise select an appropriate Python interpreter first:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -c "import langchain_core; print(langchain_core.__version__)"
```

`langchain-core>=0.3.0` is already a dependency in the root `pyproject.toml`; the editable installation installs a compatible version with the rest of the pipeline. The setup performed for this proposal used Python 3.12.13 and installed LangChain core 1.7.6. This records the observed environment, not a new version pin.

### 2. Inspect executable wiring without running documents

```bash
PYTHONPATH=src OBSERVABILITY_PROVIDER=none .venv/bin/python src/scripts/export_contract.py
PYTHONPATH=src OBSERVABILITY_PROVIDER=none .venv/bin/python src/scripts/export_contract.py --check
```

The first command prints the generated JSON contract, including nodes and edges. The second checks it against `contract/mailroom-contract.json`. Neither command invokes the pipeline on a document. Do not use `--write` merely to inspect: it updates the committed generated contract.

This is topology inspection. It does not prove routing correctness, show every guard condition, or establish whether a rarely used edge is needed.

### 3. Exercise existing routing and graph fixtures

```bash
.venv/bin/python -m pytest src/tests/test_routing.py src/tests/test_graph_contracts.py -q
.venv/bin/python -m pytest src/tests/test_docs_truth.py -q
```

Tests use mocked model clients. They do not require a paid model run. The first command exercises existing current-main scenarios; the second checks documentation. A passing finite fixture set is not proof that every possible route is correct. Test results for this planning task are reported separately with their actual exit codes.

### 4. Read the native implementation sequence

Read [PLAN.md](PLAN.md) for D0–D4. D0/D1 establish native current-main inspection and reproducible gap checks; D2 captures exact execution. D3 integrates calibration capabilities when available; D4 adds compatible per-window evidence for PR 204 comparisons.

## Planned launch flow — not runnable yet

During D0, implement and verify these interfaces, then replace this section with tested commands:

1. Install the nested Python package from this folder into the repository venv.
2. Build its `frontend/` with its committed lockfile, or use packaged assets. Keep Node optional for end users.
3. Run its standalone `serve` operation: default inspection-only mode, no watcher, no Gmail/status effects, no trace-service connection.
4. Open `/diagnostics`; the capabilities panel identifies current-main, record/replay support and evidence completeness.
5. For an already-running pipeline, mount the same tool router on this repository's API and explicitly enable diagnostic capture.

The exact console entrypoint, flags, default diagnostic port and supported Node/package versions must be documented after D0 verifies them. Do not substitute the normal `python -m api.main` for inspection-only launch: its current import path includes warmups, and its normal lifespan can start the watcher.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `No module named langchain_core` | Use `.venv/bin/python` and run the editable dependency install above |
| `fixture 'mocker' not found` | Install the parent `[dev]` extra, which includes `pytest-mock` |
| Contract check reports stale output | Inspect the graph/config change and use the repository's normal generated-contract workflow; do not suppress the check |
| `/diagnostics` is 404 today | Expected until D0 implements the screen/mount; this folder is currently documentation |
| A future replay capability is unavailable | Read the capability reason and [compatibility matrix](COMPATIBILITY.md); do not assume a branch contains PR 203's schemas |
| Existing mock pilot lacks sample assets | Disclose that limitation; do not treat a skipped pilot as evidence of correctness |

Never use real-model collection or hosted sync commands merely to launch the diagnostic screen. Offline inspection and gap fixtures must remain useful without credentials.
