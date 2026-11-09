# Quick start

## What is available now

The D0a native CLI supports workspace-preview and offline-contract inspection plus structural checks. The React workbench and diagnostic HTTP app remain planned; `/diagnostics` and `serve` are not available yet. Use [HANDOFF.md](HANDOFF.md) to find the next implementation task and [EVIDENCE.md](EVIDENCE.md) to judge its proof.

Run commands from the **repository root**, not from this tools folder. Use Python 3.11+; Python 3.12 is the environment verified for this setup. Do not use `uv sync` or `uv pip install -e .`: the root uv source entry points at a separate workspace.

### 1. Install the declared dependencies

If a repository-local `.venv` already exists, use it and skip the creation command. Otherwise select an appropriate Python interpreter first:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install -e tools/pipeline-diagnostics
.venv/bin/python -c "import langchain_core; print(langchain_core.__version__)"
```

`langchain-core>=0.3.0` is already a dependency in the root `pyproject.toml`; the editable installation installs a compatible version with the rest of the pipeline. The setup performed for this proposal used Python 3.12.13 and installed LangChain core 1.7.6. This records the observed environment, not a new version pin.

### 2. Inspect and check with the native CLI

```bash
.venv/bin/mailroom-diagnostics inspect --json
.venv/bin/mailroom-diagnostics check --json
.venv/bin/mailroom-diagnostics inspect --contract contract/mailroom-contract.json --json
.venv/bin/mailroom-diagnostics check --contract contract/mailroom-contract.json --json
```

Default inspection compiles the installed parent pipeline's graph in a fresh CLI interpreter, disables tracing and labels the result a **workspace preview**, not an active worker. Artifact mode reads the supplied bounded JSON without importing the graph; it records the artifact digest and does not claim the file matches running code. Keep stdout separate from stderr when capturing machine-readable JSON.

`check` covers endpoint consistency and reachability from the declared entry only. Exit 0 means these checks found no structural findings; exit 1 means a structural finding with a witness; exit 2 means invalid input or a runtime failure. Exit 3 is reserved for future incomplete-evidence operations. An empty findings list is not proof of complete routing, correct guards or model quality.

### 3. Compare the canonical display contract

```bash
PYTHONPATH=src OBSERVABILITY_PROVIDER=none .venv/bin/python src/scripts/export_contract.py --check
```

This checks the canonical export against `contract/mailroom-contract.json` without invoking the pipeline on a document. The legacy exporter may print diagnostic logs to stdout; use the native `inspect --json` command for machine-readable output. Do not use `--write` merely to inspect: it updates the committed generated contract.

This is topology inspection. It does not prove routing correctness, show every guard condition, or establish whether a rarely used edge is needed.

### 4. Exercise native and existing routing fixtures

```bash
.venv/bin/python -m pytest src/tests/test_routing.py src/tests/test_graph_contracts.py -q
.venv/bin/python -m pytest tools/pipeline-diagnostics/tests src/tests/test_contract_export.py src/tests/test_ci_tests.py -q
.venv/bin/python -m pytest src/tests/test_docs_truth.py -q
```

Tests use mocked model clients. They do not require a paid model run. The first command exercises existing current-main scenarios, the second checks the native package/exporter/CI planner, and the third checks documentation. A passing finite fixture set is not proof that every possible route is correct. Results are reported separately with their actual exit codes.

### 5. Resume the implementation sequence

Read [PLAN.md](PLAN.md) for D0–D4 and follow the task IDs in [HANDOFF.md](HANDOFF.md). D0a starts inspection/checks; D0.3/D0.4 complete app/provenance/packaging work before D1's graph. D2 captures exact execution, D3 adds compatible calibration adapters, and D4 covers per-window comparison and delivery hardening.

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
| `/diagnostics` is 404 today | Expected until D0.3 and the React delivery tasks implement the screen/mount; use the native CLI now |
| A future replay capability is unavailable | Read the capability reason and [compatibility matrix](COMPATIBILITY.md); do not assume a branch contains PR 203's schemas |
| Existing mock pilot lacks sample assets | Disclose that limitation; do not treat a skipped pilot as evidence of correctness |

Never use real-model collection or hosted sync commands merely to launch the diagnostic screen. Offline inspection and gap fixtures must remain useful without credentials.
