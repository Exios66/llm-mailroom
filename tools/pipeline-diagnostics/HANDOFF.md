# Implementation handoff and checkpoint ledger

This file is the ordered work queue for the native workbench. [PLAN.md](PLAN.md) is the full design; [EVIDENCE.md](EVIDENCE.md) defines acceptable proof. The draft PR must embed both the full plan and this queue so an agent can resume from the PR alone. A checked task requires evidence for its specific acceptance criteria; committed scaffolding is not completion of a phase.

## Resume protocol

1. Read repository `CLAUDE.md`, the three `.claude/rules/` files, this file, the plan and evidence contract. User preference overrides older model-tier examples and agent `model:` frontmatter for this undertaking: use appropriately scoped inexpensive agents such as DeepSeek 4.1 Flash; do not use Sonnet 4.6.
2. Inspect `git status --short`, the current branch and HEAD, the draft PR head and latest review comments. Preserve unrelated edits. Continue the designated feature branch `feat/pipeline-diagnostics-workbench`; do not reset it to a staged PR or copy a sibling repository into it.
3. Identify the first unchecked task whose dependencies are satisfied. Claim that task in the PR's current handoff section with agent/session, exact base SHA, owned paths and start time. If another agent owns it, pick independent work; do not duplicate active work.
4. Install the parent package with pip, then this nested package. Run the quick start and the task's focused checks before changing anything. Reproduce a claimed defect before fixing it.
5. Implement one coherent task. Keep tool-specific code, fixtures and tests here; edit root files only for shared pipeline hooks, generated-contract integration, authentication, packaging or test discovery. Follow the matching repository skill before touching graph/tracing/provider code.
6. Commit the task, review it using the repository's required independent rounds, fix findings, then re-run relevant gates on the final clean commit. Keep raw output, exit codes and artifact hashes. Do not skip or loosen tests to obtain a pass.
7. Update this ledger, quick start, compatibility notes and the PR's current handoff. Link each completed task to its implementation commit, test names, evidence run and any remaining limitation. If editing documentation makes verification stale, re-run the documentation gates on the new final commit.
8. On interruption, record the exact last good SHA, uncommitted paths, command/process still running, unresolved findings, next action and evidence location. Explicitly say whether a task is implemented, verified, incomplete or blocked. Never infer another agent's job completed merely because its files appeared.

## Current checkpoint

The first implementation checkpoint is **D0a**, the installable snapshot/check CLI and test-discovery foundation, delivered in PR 208. The standalone read-only HTTP app (D0.3) is delivered as a stacked PR layered on D0a. Each layer's commit-specific Evidence section is the authoritative verification record for its checkpoint. The original design commit is `133e971`; it delivered documents, not an application.

D0a means D0.1 and D0.2, checked below. Their verification is indexed by tested commit in PR 208. D0.3, D0.4a and D0.4b are checked with their own layered PRs. D0 is complete when D0.4b lands; the React screen, live telemetry, replay and calibration remain separate tasks below. No review, live tracing or calibration behavior is changed by the initial CLI checkpoint.

## Ordered tasks

### D0.1 — Installable current-main snapshot and check CLI

**Depends:** design; **owns:** nested package, CLI/models/checks, shared compiled-graph export helper.

- [x] Install the nested package from `backend/` with its console entrypoint and declared dependencies; parent mailroom remains the pipeline distribution.
- [x] Derive exact node IDs, span metadata and edges from the compiled graph through the same importable helper as the original exporter. Preserve the generated display contract byte-for-byte.
- [x] `inspect --json` emits a workspace-preview snapshot with explicit provenance and graph hash, in a fresh interpreter with tracing disabled. No API/watcher import, pipeline execution, LLM call or warmup is permitted.
- [x] `inspect --contract PATH --json` reads bounded, validated JSON offline and identifies it as an artifact. Never claim it is the running worker's graph.
- [x] `check` reports endpoint consistency and entry reachability with stable finding IDs and witness data. State that this coverage excludes routing predicates, semantic intent and execution completeness.
- [x] Prove valid pure JSON, malformed/oversized/version errors, changed fixture topology, dangling endpoint and unreachable-node findings, preserved shared-span node identities, and import/help side-effect boundaries.

**Acceptance:** install/import smoke; CLI stdout parses as one JSON document; structural fixtures fail before correction and clear afterward; exporter parity; no real-model/network path in tests. Exit 0 means only the declared checks found nothing; it is not a pipeline safety certificate.

### D0.2 — Root test and CI integration

**Depends:** D0.1 test root; **owns:** root pytest configuration, `src/scripts/ci_tests.py`, its existing unit tests, CircleCI installation.

- [x] Include nested tests in default pytest discovery and explicit full CI selections.
- [x] Map tool changes to nested tests; deleted/unrecognized inputs conservatively retain full validation.
- [x] Install the tool in CircleCI after installing the root package.
- [x] Prove affected and full plans select the nested suite and counts/cache scopes remain correct. Nested tests need their own hermetic fixtures: root `src/tests/conftest.py` is not inherited.

**Acceptance:** planner regression red-before/green-after, all existing planner tests, nested collection, and no accidental bypass of root smoke/full backstops. A documentation-only PR check is not evidence of full suite completion.

### D0.3 — Standalone inspection app and native API mount

**Depends:** D0.1; **owns:** nested app/API plus minimal side-effect-free root auth extraction and opt-in mount.

- [x] Construct a standalone inspection app without importing `api.main`, which currently starts warmups at import.
- [x] Reuse existing bearer/loopback authorization through an import-safe shared helper; preserve current producer security behavior and its tests.
- [x] Add capabilities/snapshot/findings routes under `/v1/diagnostics` and the `/diagnostics` mount. Keep the standalone inspection launch independent of the normal watcher lifecycle.
- [x] Default to no watcher, no Gmail/status messages, no relations effects and no tracing-service initialization. Make source mode and unavailable features visible.
- [x] Test unauthenticated loopback rules, public/forwarded requests, bearer rotation, missing assets, import side effects, and graph-construction failure responses.

**Acceptance:** real HTTP smoke on loopback plus test-client coverage; no background processing/hosted calls when opening the tool; unchanged producer auth behavior. Do not invent a permissive diagnostic authentication scheme.

**Evidence (this layered PR, refreshed by its PR body):** `src/api/auth.py` is the import-safe shared policy and `api.main` re-exports it unchanged; nested `test_app.py` (12) and root `test_api_diagnostics_mount.py` (3) cover token/loopback rules, sanitized failures, inert import, placeholder UI and the opt-in mount; 210 focused tests pass. Loopback smoke: `/health`, `/v1/diagnostics/{capabilities,snapshot}`, `/diagnostics` = 200; token set ⇒ missing/wrong bearer 401, correct 200; `serve --host 0.0.0.0` without a token exits 1 with a refusal. Import probe shows no `api.main`, watcher, Gmail, status or graph import when constructing the app.

### D0.4a — Packaged assets and provenance hashes

**Depends:** D0.1/D0.3; **owns:** nested package-data, asset modes and source/policy hashes.

- [x] Include built static assets through explicit nested package-data rules; source and missing-assets modes remain usable.
- [x] Verify isolated installed wheels and HTTP asset retrieval, not just editable imports. Test parent/tool interoperability without checkout-only `scripts` imports.
- [x] Add code/routing/taxonomy/effective-policy hashes and distinguish workspace-file provenance from an offline artifact. Unsupported provenance is explicit (`config_source: unavailable`), not filled from guesswork.
- [x] Keep the graph hash and source hashes separate: a hash detects a moved seam, it does not prove routing semantics.

**Acceptance:** installed artifact smoke, stable/hash-changing fixtures and asset-mode coverage. Evidence is recorded in the layer's PR body; the committed `.keep`-style static README keeps the wheel's `static/` directory present until D1.1 adds generated assets.

**Evidence (this layered PR):** `test_provenance.py` (8) covers deterministic group digests, rename/content sensitivity, missing inputs, canonical effective-policy hashing, workspace `config_source: workspace-files`, artifact `config_source: artifact` and the declared seam list; `test_assets.py` (3) covers built-index serving with SPA fallback, traversal fallback and the packaged `static/README.md`. An isolated wheel was built, inspected (`mailroom_diagnostics/static/README.md` present), installed into a fresh venv with the parent editable install, and smoke-tested: `/health` 200, `/v1/diagnostics/capabilities` 200, `/diagnostics` build-instructions page, artifact inspect 200, workspace inspect reporting `workspace-files` with `contract/effective_policy/implementation/routing/taxonomy/topology` hashes.

### D0.4b — Workspace preview refresh

**Depends:** D0.4a; **owns:** fresh-process regeneration and atomic preview publication.

- [x] Regenerate workspace previews in fresh processes after debounced edits; validate then publish atomically. Keep last valid preview with an error label after an invalid edit.
- [x] Do not hot-reload an active production worker or mutate its configuration caches.

**Evidence (this layered PR):** `test_preview.py` (8) covers atomic publication/error clearing, last-good preservation on failure and invalid output, staleness from source mtime, debounced watch rebuilds, seam-only invalidation, a real fresh-process workspace build and the `preview --json` CLI contract. The preview store writes only under the configured preview directory; `serve` never starts the refresher, so no live worker or config cache is touched.

**Acceptance:** invalid-save recovery and mixed preview/worker revision fixtures. D0 is complete only when D0.1–D0.4b are verified.

### D1.1 — Accessible React graph and inspector

**Depends:** D0.3/D0.4; **owns:** `frontend/` and synthetic graph fixtures.

- [x] Implement the React Flow graph, cyclic retry paths, stable layout, search/selection, source inspector and provenance strip from snapshot data.
- [x] Provide keyboard-operable connection/path list equivalents, visible focus, reduced motion, touch targets, light/dark contrast and meaningful labels.
- [x] Support 360px, tablet and desktop without requiring gestures or page-wide horizontal scrolling. Preserve focus/selection/zoom during ordinary updates.
- [x] Demonstrate add/remove-node/edge fixture changes without frontend inventory edits. Use a fixture-only builder or disposable checkout; keep production wiring untouched.

**Acceptance:** desktop/mobile screenshots, keyboard/screen-reader walkthrough and interaction recording with synthetic data. Build the locked frontend dependency graph. React Flow support alone is not proof of application accessibility.

**Evidence (this layered PR):** locked `npm ci`; `npm run test` 13 passed (layout determinism, self-loops, duplicate pairs, added/removed edges, search, list selection, deep links, token header); `npm run typecheck` and `npm run build` clean. Browser walkthrough on a real `serve` instance: 16 nodes/46 edges, 0 console errors after the favicon fix; skip link focuses `#connections`; keyboard Enter/Space selects nodes and updates the inspector; Refresh preserves viewport transform and selection; reduced-motion emulation reports no edge animation; 360px viewport has no page-wide horizontal overflow and defaults to the card list. Screenshots/recording are linked from the layer PR (synthetic snapshot only).

### D1.2 — Reproducible current-pipeline gap scenarios

**Depends:** D0.1, D1.1; **owns:** findings rules, expectation declarations and bounded fixtures.

- [ ] Add bounded real-router scenarios for threshold boundaries, unknown/null outputs, failed/hollow windows, retry exhaustion, quality-gate ordering, review causes and state handoff requirements.
- [ ] Distinguish structural violation, failing expectation, untested scenario, unobserved runtime path and proposed connection.
- [ ] Emit a minimal witness, affected IDs, exact revision and reproducible command for each confirmed finding.
- [ ] Show Gmail triage, BERT substeps, asynchronous terminal effects and abort/review boundaries without inventing LangGraph edges or flagging valid extraction skips.

**Acceptance:** deliberate defects detected, corrected fixtures clear, and no “unvisited therefore dead” or “finite matrix therefore all Python states safe” claims. Existing-main holes must reproduce before being marked confirmed.

### D2.1 — Exact execution and routing instrumentation

**Depends:** D0/D1; **owns:** small node/router hooks plus nested event schema/broker.

- [ ] Emit start/completion/error/interrupt/resume/route/terminal events with exact `node_id`, invocation ID, execution ID, sequence and originating snapshot ID.
- [ ] Capture router decisions once; do not call mutating routers again to explain them. Keep raw confidence distinct from post-guard confidence and unknown values.
- [ ] Record only allowlisted metadata; never dump DocumentState, source documents, credentials or gold into the feed.
- [ ] Preserve existing tracing names/types and business audit semantics; diagnostic transport failure must fail open to processing.

**Acceptance:** mock paths including retries and review resume match exact events; unchanged contract/experiment gates; no extra model call; bounded overhead evidence.

### D2.2 — Resumable live transport and standalone workers

**Depends:** D2.1; **owns:** nested journal/collector/SSE and frontend reducer.

- [ ] Support standalone watcher processes; do not assume API memory is shared.
- [ ] Bootstrap snapshot plus watermark, deduplicate/order by causal identity, reconnect with bounded history and explicit gaps; support polling fallback.
- [ ] Bound subscriber queues, retention, disk growth and slow-client behavior. Include cancellation/shutdown cleanup.
- [ ] Authenticate streaming without long-lived tokens in URLs. Distinguish live/stale/disconnected/snapshot/replay.

**Acceptance:** lost/duplicate/reordered events, expired cursors, source loss, process restart and mixed-worker revisions; slow clients do not backpressure the pipeline. Native debugging MVP closes after D0–D2 pass.

### D3.1 — Capability adapters and revision comparisons

**Depends:** D0–D2; **owns:** nested adapters and revision fixtures.

- [ ] Probe actual schemas/features; baseline launch must work when RunRecord/replay modules are absent. Equal contract version strings do not establish equal topology.
- [ ] Generate trusted artifacts separately for current main, PR 203 and PR 204; record exact SHAs, merge-base and source hashes.
- [ ] Preserve the PR 203/204 base-drift distinction in COMPATIBILITY.md. PR 204's older raw head is not a latest-203 adoption candidate.
- [ ] Treat The-Mailroom PR 71 as downstream context with intentional staged-contract provenance, not a runtime dependency.

**Acceptance:** compatible adapters work; missing/unknown schemas disable only affected operations; no automatic checkout/execution of arbitrary remote PR code.

### D3.2 — Isolated calibration/replay jobs

**Depends:** D3.1 and compatible PR 203 evidence; **owns:** nested fixed job protocol and comparison UI.

- [ ] Launch each policy replay in a fresh interpreter, with bounded time/resources/input and approved artifacts; no process reuse or production cache patches.
- [ ] Reuse PR 203 replay/Dojo metrics and guard definitions. Preserve unobserved branches; do not duplicate formulas in React.
- [ ] Show sample sizes, splits, confidence intervals, sample shortfalls, raw/post-guard attempts and actual-versus-replayed paths.
- [ ] Keep oracle/synthetic/mock/unsplit/real held-out evidence distinct. Calibration sliders never promote live taxonomy.

**Acceptance:** sequential/concurrent job isolation, timeout/cancellation, matching harness outputs, missing-branch cases and immutable effective-policy evidence. No live LLM dispatch.

### D4.1 — PR 204 window evidence and controlled merge comparison

**Depends:** D3.2 plus compatible collection; **owns:** versioned per-window observation seam and trusted merge comparison.

- [ ] Collect window coverage, canonical-field presence, success/failure, confidence and strategy version without document/field values.
- [ ] Compare productive+empty, all-empty, failed, malformed and false-negative-empty cases through trusted revision-specific code.
- [ ] Flag changed implementation even with identical edge lists. Refuse reconstruction from aggregate-only records.
- [ ] Preserve PR 204's real held-out evidence/guard requirements; synthetic correctness does not justify more permissive auto-archival.

**Acceptance:** compatible/incompatible observation tests, routing consequences with complete downstream observations, and explicit unavailable comparisons when information is missing.

### D4.2 — Offline bundles, performance and promotion

**Depends:** preceding feature tasks; **owns:** bundle protocol, packaging/docs and final evidence.

- [ ] Export/import bounded versioned redacted bundles with hashes, stable finding IDs and source/replay status; reject malicious paths and oversized/corrupt inputs.
- [ ] Measure latency/overhead/memory/contention on documented fixed workloads; proposed targets are not achieved numbers.
- [ ] Finish responsive/a11y/manual verification, installed artifact behavior and CircleCI build/test evidence.
- [ ] Run the full applicable Python suites on the final commit, plus all graph/config/generated checks, and disclose every meaningful skip/environment failure.
- [ ] Resolve review findings and downstream contract follow-ups, update runnable quick starts, and mark the PR ready only after its actual implemented scope is complete.

**Acceptance:** independently reproducible evidence index, final clean commit, required reviews and no unresolved blocking/major defects. Any deliberately deferred feature remains an explicit unchecked task with a separately agreed delivery boundary.

## Stop/resume record template

Copy this into the PR when handing off; replace every placeholder with observed facts:

```text
Task / state:
Agent/session / UTC time:
Branch / exact HEAD / comparison base:
Owned files / unrelated edits preserved:
Implemented behavior:
Last successful command / exit / raw summary:
Evidence run ID / immutable artifact URLs / SHA-256:
Running process or delegated task IDs:
Unresolved findings / missing dependencies:
Next exact action:
Readiness: draft | ready (with proof)
```
