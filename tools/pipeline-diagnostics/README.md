# Pipeline diagnostics

Repository-native debugging and diagnostic tooling for llm-mailroom.

**Current status: D0 complete, D1.1 (accessible React graph) implemented, and live-corpus run journals/replay wired into the viewer; the workbench continues with D1.2 gap scenarios.** The native package supports compiled-graph and offline-contract inspection, structural endpoint/reachability checks, a read-only HTTP app (`mailroom-diagnostics serve`) that is also mountable on the producer API, source/policy provenance hashes, atomic fresh-process preview refresh, and an accessible React/React Flow screen at `/diagnostics`. Recorded run journals, experiment summaries and path playback are implemented; live SSE execution events and calibration remain later tasks.

## Start here

- [Quick start](QUICKSTART.md): install dependencies, inspect the current compiled graph and run existing routing checks.
- [Compatibility](COMPATIBILITY.md): what works on current main, what PR 203 enables, and what evidence PR 204 needs.
- [Implementation plan](PLAN.md): architecture, API/data contracts, live updates, gap detection, accessible React design, delivery phases and verification gates.
- [Agent handoff](HANDOFF.md): ordered tasks, ownership, dependencies, acceptance checks and interruption/resume instructions.
- [Evidence contract](EVIDENCE.md): commit-scoped proof and independent post-hoc verification requirements.

## Intended native experience

Run the workbench directly from this checkout. Inspect executable connections, select a mock or observed run, follow its decisions, reproduce a finding, and export an offline bundle. Agents use the same diagnostic model through JSON and the CLI. Neither path needs another repository or hosted tracing.

Keep these distinctions visible:

- Workspace graph/config preview versus the revision actually loaded by a worker.
- Declared connections versus the path a particular execution took.
- Confirmed routing defects versus untested branches or missing observations.
- Wiring changes versus policy or confidence-aggregation changes.
- Current capabilities versus staged features that have not landed.

## Folder ownership

All tool-specific implementation, tests, fixtures, package configuration and documentation belong in this folder. Existing root pipeline/API modules receive only the hooks needed to observe execution or mount the native screen. The pipeline remains the authority for routing; the generated display contract remains the authority for its exported topology.

Folder layout:

```text
tools/pipeline-diagnostics/
  README.md
  QUICKSTART.md
  COMPATIBILITY.md
  PLAN.md
  HANDOFF.md
  EVIDENCE.md
  pyproject.toml
  backend/mailroom_diagnostics/
    app.py                 # standalone inspection app; never imports api.main
    api.py                 # router also mountable by the normal API
    static/                # generated React build (README only in the source tree)
    ...                    # schemas, snapshots, findings, CLI and job protocol
  frontend/                # React/TypeScript screen and committed lockfile
  tests/                   # explicitly collected by CI
    fixtures/              # synthetic defects and revision-specific contracts
```

## Live corpus, run journals and replay

`scripts/pull_live_manifest.py` resolves the newest tagged `Lucius-Morningstar/mailroom-dataset` release, verifies the release manifest and every parquet/content hash, and emits the pipeline manifest shape (no document text). `scripts/smoke_live_documents.py` fetches the selected real documents, runs the full graph (mock by default), and writes one run journal plus an experiment summary. `--replay <journal>` re-runs the same document and diffs the node sequence and outcome.

The viewer's **Recorded runs** panel loads journals from `<base>/diagnostics/runs` and plays the recorded node path (numbered nodes, highlighted transitions); **Experiments** loads summaries and jumps to their runs. Playback reads the journal; only the CLI replay re-executes the pipeline. The routes are `GET /v1/diagnostics/runs`, `/runs/{execution_id}`, `/experiments`, `/experiments/{experiment_id}`.

Live documents are opt-in: `MAILROOM_LIVE_TESTS=1` runs the real-corpus routing/replay test. Screenshots and journals never embed document text.

## Workspace preview refresh

`mailroom-diagnostics preview` builds the snapshot in a separate interpreter and atomically publishes `preview.json`, `last-good.json` and `error.json` under `MAILROOM_DIAGNOSTICS_PREVIEW_DIR` (or `<MAILROOM_BASE_DIR>/diagnostics`; default `data/diagnostics`). A failed or invalid rebuild never replaces the last valid preview and records a visible error. `preview --watch` debounces edits to the declared source/config seams before rebuilding. A preview always describes edited files, never a running worker's active graph.

## Read-only HTTP app

`mailroom-diagnostics serve` starts a diagnostics-only FastAPI app on loopback (default `127.0.0.1:8100`). It never imports `api.main` and never starts the watcher, Gmail intake, status mail, relations scans, document processing or a tracing backend.

| Route | Purpose |
| --- | --- |
| `GET /v1/diagnostics/capabilities` | Supported modes, read-only status and explicitly unavailable features |
| `GET /v1/diagnostics/snapshot` | Current workspace-preview graph and provenance |
| `GET /v1/diagnostics/findings` | Implemented structural checks with stable finding IDs |
| `GET /health` | Public liveness probe |
| `GET /diagnostics` | Build-instructions page until the React assets land (D1.1) |

Authorization is the producer's bearer/loopback rule, shared through `api.auth`: an off-loopback bind requires `MAILROOM_API_TOKEN` or `MAILROOM_API_TOKENS`. Set `MAILROOM_DIAGNOSTICS_MOUNT=1` to mount the same read-only routes on `python -m api.main`; the mount is off by default and a missing package never breaks API startup.

LangChain core is already declared by the parent project. Install the project's development dependencies rather than adding a duplicate root requirement. The nested package declares its own direct dependencies. Install the parent from this checkout first; the unrelated PyPI project named `mailroom` is not a tool dependency.

First useful milestone: current-main topology, source-linked checks and reproducible missing-route fixtures. Exact mock/live execution follows, then staged calibration adapters. PR 204 is not a prerequisite for the tool.

The nested suite is registered in root pytest discovery and the CI planner, with its own hermetic fixtures. A full explicit Python run covers both `src/tests` and this folder's `tests`; a scoped run remains scoped evidence. See the PR's tested SHA/results before relying on a checkpoint.

Related downstream work: [The-Mailroom PR 71](https://github.com/Exios66/The-Mailroom/pull/71) handles adoption of PR 203's contract and review behavior. It is complementary context, not the home or a dependency of this native tool.
