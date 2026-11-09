# Pipeline diagnostics

Repository-native debugging and diagnostic tooling for llm-mailroom.

**Current status: planning and setup documentation.** The React screen, diagnostic CLI and diagnostic API described here are not implemented yet. Existing pipeline inspection and mock tests can be used now; see the quick start. Do not expect `/diagnostics` to exist on current main.

## Start here

- [Quick start](QUICKSTART.md): install dependencies, inspect the current compiled graph and run existing routing checks.
- [Compatibility](COMPATIBILITY.md): what works on current main, what PR 203 enables, and what evidence PR 204 needs.
- [Implementation plan](PLAN.md): architecture, API/data contracts, live updates, gap detection, accessible React design, delivery phases and verification gates.

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

Planned layout, except for the documentation already present:

```text
tools/pipeline-diagnostics/
  README.md
  QUICKSTART.md
  COMPATIBILITY.md
  PLAN.md
  pyproject.toml
  backend/mailroom_diagnostics/
    app.py                 # standalone inspection app; never imports api.main
    api.py                 # router also mountable by the normal API
    static/                # generated, packaged React build
    ...                    # schemas, snapshots, findings, CLI and job protocol
  frontend/                # React/TypeScript source and committed lockfile
  tests/                   # explicitly collected by CI
  fixtures/                # synthetic defects and revision-specific contracts
```

LangChain core is already declared by the parent project. Install the project's development dependencies rather than adding a duplicate root requirement. The future nested package will also declare dependencies that it imports directly.

First useful milestone: current-main topology, source-linked checks and reproducible missing-route fixtures. Exact mock/live execution follows, then staged calibration adapters. PR 204 is not a prerequisite for the tool.

When implementation starts, D0 must register the nested suite in the root pytest configuration and CI planner, with hermetic fixtures. Today's default root pytest and CI full-run selection cover `src/tests`, not a future test suite under this folder.

Related downstream work: [The-Mailroom PR 71](https://github.com/Exios66/The-Mailroom/pull/71) handles adoption of PR 203's contract and review behavior. It is complementary context, not the home or a dependency of this native tool.
