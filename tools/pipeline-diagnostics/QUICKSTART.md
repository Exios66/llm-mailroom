# Quick start

## What is available now

The native CLI supports workspace-preview and offline-contract inspection plus structural checks. D0 adds the read-only standalone HTTP app (`serve`), the `/v1/diagnostics` routes, provenance hashes, atomic preview refresh and an opt-in mount on the producer API. D1.1 adds the accessible React/React Flow screen at `/diagnostics`; the live-corpus runner adds pinned real-document smoke tests, run journals and run/experiment loading in the screen. Without built assets the app still serves a build-instructions page and the JSON API keeps working. Use [HANDOFF.md](HANDOFF.md) to find the next implementation task and [EVIDENCE.md](EVIDENCE.md) to judge its proof.

Run commands from the **repository root**, not from this tools folder. Use Python 3.11+; Python 3.12 is the environment verified for this setup. The commands assume a POSIX shell (macOS, Linux, or WSL); on native Windows use the `.venv\Scripts\` executables instead of `.venv/bin/`. Inline `VAR=value` assignments below are also POSIX: on PowerShell set `$env:PYTHONPATH='src'; $env:OBSERVABILITY_PROVIDER='none'` first, or run the step in WSL. Do not use `uv sync` or `uv pip install -e .`: the root uv source entry points at a separate workspace.

### 1. Install the declared dependencies

If a repository-local `.venv` already exists, use it and skip the creation command. Otherwise select an appropriate Python interpreter first:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install -e tools/pipeline-diagnostics
.venv/bin/python -c "import langchain_core; print(langchain_core.__version__)"
```

Install the parent from this checkout first. The tool does not depend on the unrelated PyPI package named `mailroom`; artifact-only inspection also works without the parent.

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

For a file-backed preview that a long-lived client can read without compiling in-process:

```bash
.venv/bin/mailroom-diagnostics preview --json
.venv/bin/mailroom-diagnostics preview --watch
```

The fresh interpreter writes `preview.json`, `last-good.json` and `error.json` under `MAILROOM_DIAGNOSTICS_PREVIEW_DIR` (or `<MAILROOM_BASE_DIR>/diagnostics`; default `data/diagnostics`). Writes are atomic, a failed or invalid rebuild keeps the last valid preview and records the error, and `--watch` rebuilds only after source/config edits settle (default 0.5s poll, 0.3s quiet period). This is still a workspace preview, never a running worker.

### 3. Live corpus manifest and real-document smoke

The live corpus loader resolves the newest tagged `Lucius-Morningstar/mailroom-dataset` release (v9.3 at the time of writing), verifies the release checksums and every `content_sha256`, and writes the pipeline manifest shape. It never embeds document text.

```bash
PYTHONPATH=src python src/scripts/pull_live_manifest.py --live --per-class 3 --json
PYTHONPATH=src python src/scripts/pull_live_manifest.py --check src/tests/fixtures/live_manifest_sample.csv
PYTHONPATH=src python src/scripts/smoke_live_documents.py --manifest src/tests/fixtures/live_manifest_sample.csv --per-class 1 --base data/live_smoke/base --out data/live_smoke/report
```

The smoke runner fetches the selected real documents, verifies their recorded hashes, runs the full graph (deterministic mock LLM by default; `--real` uses the provider), and writes:

- one journal per document under `<base>/diagnostics/runs/` with the exact node ingress order, transitions and terminal outcome;
- an experiment summary under `<base>/diagnostics/experiments/` and the requested output directory.

Replay a recorded journal (re-runs the same real document and diffs the path/outcome):

```bash
PYTHONPATH=src python src/scripts/smoke_live_documents.py --replay data/live_smoke/base/diagnostics/runs/<journal>.json --manifest src/tests/fixtures/live_manifest_sample.csv --out data/live_smoke/replay
```

The network-gated real-corpus test is `MAILROOM_LIVE_TESTS=1 .venv/bin/python -m pytest src/tests/test_live_corpus_smoke.py -q`. Without the variable it is skipped; it is never a synthetic-document substitute. The viewer loads recorded journals and experiment summaries from the same base directory (Recorded runs / Experiments panels).

### 4. Serve the read-only app (optional)

```bash
.venv/bin/mailroom-diagnostics serve
# http://127.0.0.1:8100/diagnostics      built React screen (build-instructions page without assets)
# http://127.0.0.1:8100/v1/diagnostics/capabilities
# http://127.0.0.1:8100/v1/diagnostics/snapshot
# http://127.0.0.1:8100/v1/diagnostics/findings
# http://127.0.0.1:8100/v1/diagnostics/runs
# http://127.0.0.1:8100/v1/diagnostics/experiments
# http://127.0.0.1:8100/health

# Load the journals/experiments from the smoke base while serving:
MAILROOM_BASE_DIR=data/live_smoke/base \
MAILROOM_DIAGNOSTICS_STATIC=tools/pipeline-diagnostics/frontend/dist \
  .venv/bin/mailroom-diagnostics serve
```

`serve` constructs a diagnostics-only FastAPI app. It never imports `api.main`, never starts the watcher, Gmail intake, status mail, relations scans or a tracing backend, and processes no documents. Defaults are loopback `127.0.0.1:8100` (`MAILROOM_DIAGNOSTICS_HOST` / `MAILROOM_DIAGNOSTICS_PORT`); an off-loopback bind requires `MAILROOM_API_TOKEN` or `MAILROOM_API_TOKENS`, the same rule as the producer. Routes reuse the producer bearer/loopback check.

To build the React screen (Node 22 is the verified toolchain):

```bash
cd tools/pipeline-diagnostics/frontend
npm ci
npm run build
cd ../../..
MAILROOM_DIAGNOSTICS_STATIC=tools/pipeline-diagnostics/frontend/dist \
  .venv/bin/mailroom-diagnostics serve
```

`npm run package:static` copies `index.html` and `assets/` into the nested package's `static/` directory for a packaged wheel. Do not hand-edit generated assets. Without a build, `/diagnostics` shows the build-instructions page; the API, CLI and preview refresh remain fully usable.

To mount the same read-only routes on the normal producer API, set `MAILROOM_DIAGNOSTICS_MOUNT=1` before starting it. The mount is off by default; a missing nested package logs a warning and never fails API startup.

### 5. Compare the canonical display contract

```bash
PYTHONPATH=src OBSERVABILITY_PROVIDER=none .venv/bin/python src/scripts/export_contract.py --check
```

This checks the canonical export against `contract/mailroom-contract.json` without invoking the pipeline on a document. The legacy exporter may print diagnostic logs to stdout; use the native `inspect --json` command for machine-readable output. Do not use `--write` merely to inspect: it updates the committed generated contract.

This is topology inspection. It does not prove routing correctness, show every guard condition, or establish whether a rarely used edge is needed.

### 6. Exercise native and existing routing fixtures

```bash
.venv/bin/python -m pytest src/tests/test_routing.py src/tests/test_graph_contracts.py -q
.venv/bin/python -m pytest tools/pipeline-diagnostics/tests src/tests/test_contract_export.py src/tests/test_ci_tests.py -q
.venv/bin/python -m pytest src/tests/test_docs_truth.py -q
```

Tests use mocked model clients. They do not require a paid model run. The first command exercises existing current-main scenarios, the second checks the native package/exporter/CI planner, and the third checks documentation. A passing finite fixture set is not proof that every possible route is correct. Results are reported separately with their actual exit codes.

### 7. Resume the implementation sequence

Read [PLAN.md](PLAN.md) for D0–D4 and follow the task IDs in [HANDOFF.md](HANDOFF.md). D0a starts inspection/checks; D0.3/D0.4 complete app/provenance/packaging work before D1's graph. D2 captures exact execution, D3 adds compatible calibration adapters, and D4 covers per-window comparison and delivery hardening.

## Launch flow

1. Install the nested Python package from this folder into the repository venv (step 1).
2. For the React screen, build `frontend/` with its committed lockfile, or use packaged assets. Node stays optional for end users.
4. Run the standalone `serve` operation (step 4): inspection-only mode, no watcher, no Gmail/status effects, no trace-service connection.
5. Open `/diagnostics`; the screen identifies the source as a workspace preview, lists unavailable capabilities explicitly, and loads recorded runs/experiments from `MAILROOM_BASE_DIR`.
6. For an already-running pipeline, set `MAILROOM_DIAGNOSTICS_MOUNT=1` and mount the same read-only router on this repository's API.

Do not substitute the normal `python -m api.main` for inspection-only launch: its import path includes warmups, and its normal lifespan can start the watcher.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `No module named langchain_core` | Use `.venv/bin/python` and run the editable dependency install above |
| `fixture 'mocker' not found` | Install the parent `[dev]` extra, which includes `pytest-mock` |
| Contract check reports stale output | Inspect the graph/config change and use the repository's normal generated-contract workflow; do not suppress the check |
| `/diagnostics` shows build instructions | Build `frontend/` (step 3) or set `MAILROOM_DIAGNOSTICS_STATIC`; the JSON API and CLI are usable without it |
| A future replay capability is unavailable | Read the capability reason and [compatibility matrix](COMPATIBILITY.md); do not assume a branch contains PR 203's schemas |
| Existing mock pilot lacks sample assets | Disclose that limitation; do not treat a skipped pilot as evidence of correctness |

Never use real-model collection or hosted sync commands merely to launch the diagnostic screen. Offline inspection and gap fixtures must remain useful without credentials.
