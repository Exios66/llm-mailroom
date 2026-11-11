# Pipeline diagnostics

Repository-native debugging and diagnostic tooling for llm-mailroom.

**Current status: D0 complete, D1.1 (accessible React graph), recorded-run playback, and read-only audit inspection are implemented.** The audit panel follows the selected run or supports independent document lookup and paginated discovery. The native package supports compiled-graph and offline-contract inspection, structural endpoint/reachability checks, a read-only HTTP app (`mailroom-diagnostics serve`) that is also mountable on the producer API, source/policy provenance hashes, atomic fresh-process preview refresh, and an accessible React/React Flow screen at `/diagnostics`. Live SSE execution events, additional gap scenarios and calibration remain later tasks.

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
    audit.py               # read-only audit reader, verifier and redaction
    seed_audit_demo.py     # synthetic audit store for demos/tests (explicit --out only)
    static/                # generated React build (README only in the source tree)
    ...                    # schemas, snapshots, findings, CLI and job protocol
  frontend/                # React/TypeScript screen and committed lockfile
  tests/                   # explicitly collected by CI
    fixtures/              # synthetic defects and revision-specific contracts
```

## Audit inspection (React, API and CLI)

Open **Audit record** from the section links. By default it follows the selected recorded run's document ID; turn off **Follow selected run document** to choose a listed document or enter an ID. Previous/next pages discover older records. **Refresh audit and list**, the workbench Refresh button, and workspace polling reload the active audit; a refresh tick that arrives while the current read is still in flight is coalesced onto it, so a slow response still lands. Row attempts and addendum attempts are displayed separately. On narrow screens the controls and audit panel appear before the graph and connection table; the connection list remains the keyboard-readable graph equivalent.

The diagnostics layer reads the audit store **read-only** and reports what it can actually prove. It is deliberately independent of the writers: it supports today's per-event chain and the optional unified-row schema, and it never creates, migrates or initializes a database.

| Route | Purpose |
| --- | --- |
| `GET /v1/diagnostics/audit/documents?limit=&offset=` | Bounded, newest-first document ids from the audit tables (no raw detail) |
| `GET /v1/diagnostics/audit/documents/{doc_id}` | One document's record, derived status, redacted projection and verification verdict |

The same payloads are available to agents through `mailroom-diagnostics audit <doc_id> --json` and `mailroom-diagnostics audit --list --json` (exit 0 verified, 1 failed verification, 2 invalid input, 3 not-recorded/unavailable/unverified).

**Listing cost.** The listing returns a bounded page (`limit`/`offset`), but it groups ids with `GROUP BY doc_id` over the union of `audit_log` and `audit_log_legacy`. That aggregate is not an index-only lookup: on a large store the database may scan and aggregate both tables for every page. The per-document detail read (below) is separately bounded.

**Schema modes.** The reader detects, per request:

- `event-chain` — today's `audit_log` with many hash-chained rows per document, verified with `schemas.audit.verify_chain`.
- `unified-row` — the foundation's one-row-per-document schema: the immutable `audit_log` row, its `audit_attempts` sub-rows and `audit_addenda`, with the previous per-event rows preserved in `audit_log_legacy`. The migration marker in `audit_meta` (or a unique `doc_id` index) is what selects this mode: the auxiliary tables exist as soon as `ensure_schema` runs, before any migration, so their presence alone is never treated as a unified row. A marker that explicitly says `migrated: false` is **ambiguous**, not unified: the reader fails closed instead of treating a still-per-event store as terminal rows.
- `absent` / `unavailable` — no audit schema, a missing database file, no sync Postgres driver, or an unreadable store. Reasons are safe codes; the database path or URL is never returned.

Migrated legacy events are always surfaced as a separate `legacy` section, so "events exist" is never confused with "a unified terminal row exists". A document with only legacy events reports `record.state = "legacy-events"`.

**Verification honesty.** The viewer verifies the full, unredacted rows first:

- Unified rows and addenda are re-hashed with the foundation's `audit.writer.hash_entry` when that package is installed, otherwise with the shared `schemas.audit.compute_audit_hash` primitive (identical math; the attempt sub-rows then stay `unverified` rather than being reported as verified).
- Attempt sub-rows are hashed only through the foundation's `audit.writer.attempt_hash`; without it they are honestly `unverified`. Every returned attempt row and every parent reference is accounted for exactly once: orphans, cross-document parentage, duplicate/malformed references and unlisted rows fail.
- `seq` is not hashed, so it is checked structurally: the head row is `seq 1`, a later row is `predecessor.seq + 1`, and addenda are contiguous from `1`. A mutated `seq` therefore fails even when the hash still matches.
- More than one terminal `audit_log` row for one document is a structural failure (`duplicate-terminal-row`), never a silently chosen row: a marker-only store may lack the unique `doc_id` index, so the detail read fetches up to two rows and the listing counts the rows it reports.
- The verdict is per document: row hash, immediate predecessor link and sequence, addenda chain, attempts and the migrated legacy chain. It is **not** a whole-database chain proof.
- Every read is bounded. A truncated read (plus any unverified scope) yields `unverified`, never `verified`. When the addenda section is truncated, an attempt whose parent addendum lies beyond that read is reported as unverified — never as a fabricated orphan — while mismatches observed on the rows that were read still fail.
- All queries for one document run inside **one read-only snapshot** (explicit SQLite `BEGIN`, Postgres `REPEATABLE READ`), so a writer committing mid-verification cannot produce a false "broken chain" finding.

**Redaction.** The API returns ids, event/actor/timestamps, matter id, sequence, prev/entry hashes, derived status, attempt/addendum summaries and *decision field names*. Raw `detail` values are used only for hashing and are never serialized: they can contain document text, secrets or local paths.

**Scope boundary.** This lands the diagnostics viewer/API layer. Its parent PR supplies M2 tables/migration, M3 writers and storage-level M4 verification. M4 pending/sink collection and M5–M15 remain separate work. Reading a store whose writers have not finished is supported; writing, migrating and schema creation are outside this tool. Foundation startup migrates only with `MAILROOM_AUDIT_MIGRATE=1` on both SQLite and Postgres. The explicit `python -m storage.audit_migrate` command bypasses that flag only on SQLite; Postgres still requires it. Normal pipeline runs continue using per-event audit writes.

**Synthetic demo store.** For screenshots and local demos:

```bash
.venv/bin/python -m mailroom_diagnostics.seed_audit_demo --out /tmp/audit-demo --schema unified
DATABASE_URL="sqlite+aiosqlite:////tmp/audit-demo/mailroom.db" \
  .venv/bin/mailroom-diagnostics audit --list --json
```

The helper writes only the explicit `--out` directory (named `mailroom.db`), refuses the configured mailroom base directory, the current directory's `data/` store and the `data/` store of the repository checkout containing this package, and refuses to overwrite an existing database without `--force`. Every value is synthetic; `document_text`/`note` fields exist to prove the API redacts them.

## Live corpus, run journals and replay

`src/scripts/pull_live_manifest.py` resolves the newest tagged `Lucius-Morningstar/mailroom-dataset` release, verifies the release manifest and every parquet/content hash, and emits the pipeline manifest shape (no document text). `src/scripts/smoke_live_documents.py` fetches the selected real documents, runs the full graph (mock by default), and writes one run journal plus an experiment summary. `--replay <journal>` re-runs the same document and diffs the node sequence and outcome.

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
| `GET /v1/diagnostics/audit/documents` | Bounded audit document ids (read-only) |
| `GET /v1/diagnostics/audit/documents/{doc_id}` | Audit record, status, verification verdict (redacted) |
| `GET /health` | Public liveness probe |
| `GET /diagnostics` | React workbench when assets are built; build instructions otherwise |

Authorization is the producer's bearer/loopback rule, shared through `api.auth`: an off-loopback bind requires `MAILROOM_API_TOKEN` or `MAILROOM_API_TOKENS`. Set `MAILROOM_DIAGNOSTICS_MOUNT=1` to mount the same read-only routes on `python -m api.main`; the mount is off by default and a missing package never breaks API startup.

LangChain core is already declared by the parent project. Install the project's development dependencies rather than adding a duplicate root requirement. The nested package declares its own direct dependencies. Install the parent from this checkout first; the unrelated PyPI project named `mailroom` is not a tool dependency.

First useful milestone: current-main topology, source-linked checks and reproducible missing-route fixtures. Exact mock/live execution follows, then staged calibration adapters. PR 204 is not a prerequisite for the tool.

The nested suite is registered in root pytest discovery and the CI planner, with its own hermetic fixtures. A full explicit Python run covers both `src/tests` and this folder's `tests`; a scoped run remains scoped evidence. See the PR's tested SHA/results before relying on a checkpoint.

Related downstream work: [The-Mailroom PR 71](https://github.com/Exios66/The-Mailroom/pull/71) handles adoption of PR 203's contract and review behavior. It is complementary context, not the home or a dependency of this native tool.
