# Native pipeline diagnostics workbench — proposed implementation plan

Status: staged delivery plan. D0.1–D0.2 supply the installable inspection/check CLI and test discovery; D0.3 adds the standalone read-only HTTP app and the opt-in producer mount; D0.4a adds packaged-asset rules and source/policy provenance hashes. D0.4b and the React screen, live telemetry and calibration remain planned. Use [HANDOFF.md](HANDOFF.md) for the ordered work queue and [EVIDENCE.md](EVIDENCE.md) for verification.

The tool belongs in **llm-mailroom**, is consolidated under **`tools/pipeline-diagnostics/`**, runs from this checkout, and assists humans and agents debugging this pipeline. It must not require The-Mailroom, the integration hub, hosted tracing, or a paid model call. Its frontend, backend, schemas, CLI, fixtures, tests, packaging and documentation stay together in that subfolder. Only small integration hooks in existing pipeline/API files live elsewhere. Existing downstream visualizers can continue consuming the display contract independently.

## 1. Outcome and design decisions

Build a native **Pipeline Diagnostics** workbench at `/diagnostics`, served by this repository's FastAPI app, with four coordinated views:

| View | Question it answers |
| --- | --- |
| Graph | What is actually wired, under which configuration and code revision? |
| Runs | What happened to this document, at each attempt and routing decision? |
| Compare / calibrate | What changed between revisions or policies, and what evidence supports it? |
| Gaps | Which connections, guards, observations, or test scenarios are missing? |

The graph is derived from executable LangGraph wiring. The frontend does not maintain a second node/edge inventory or implement routing logic. Python computes diagnostics and replay; React renders those results. Dragging a node changes layout only. Adding a hypothetical connection creates an annotation/proposal; it cannot modify the running graph.

Treat these as separate layers: **declared topology**, **effective routing policy**, **observed execution**, and **expected behavior/tests**. Topology alone cannot detect behavior changes such as PR 204.

First release: inspect and diagnose. Reuse existing review-resolution endpoints where appropriate; do not add a second review engine or automatically promote thresholds.

## 2. Evidence and integration baseline

Investigation baseline: local `main` at `35ca9a4`. PRs remain separate, unmerged inputs at the revisions inspected below. Refresh their SHAs before implementation; branch names and descriptions are not immutable evidence.

| Existing source | Relevant fact | Design consequence |
| --- | --- | --- |
| `src/scripts/export_contract.py`, `_graph_section()` | Builds the graph with `MemorySaver`, reads compiled edges and wrapped node metadata | Extend this exporter and its shared internals; do not scrape a diagram or infer wiring from AST text |
| `contract/mailroom-contract.json` | Contains 14 nodes, start/end, conditional edges, spans, classes, scores, and thresholds | Useful topology foundation; most conditional labels are null and do not explain the routing predicate |
| `src/graph/build_graph.py`, `build_graph()` | Registers nodes, conditional maps, transient self-loops, entry routes | Export bindings and stable identities from this registration seam |
| `src/graph/routing.py` | Makes routing decisions; branches can attach review causes | Capture the existing router's result and resulting cause changes once; do not call it again merely to explain it |
| `src/observability/tracing.py`, `traced_node()` | Several nodes share span names; structured behavior depends on backend | Exact node identity requires explicit node IDs; span order alone cannot distinguish retries and review |
| `src/api/main.py` | Existing token checks and request/response endpoints; no producer SSE/WebSocket stream | Add native diagnostic endpoints and a transport here; reuse authentication |
| `src/pipeline/config.py` | Configuration is cached | Distinguish edited source configuration from configuration loaded by a running worker |
| `pyproject.toml` | Explicit package discovery and package-data allowlists; already declares `langchain-core>=0.3.0`; Python install has no frontend build | Reuse the repo dependency; package nested tool/assets deliberately; keep Node a development/build dependency |

[PR 203](https://github.com/Exios66/llm-mailroom/pull/203), inspected head `bee18cb0ba550c0d20043a5a04ac0814012d0454`, introduces RunRecord, policy replay, calibration, hill-climb/stress harnesses, review provenance and new human-applied report-tail routes. The new `human_review → compile_report` and `__start__ → compile_report` edges must appear when that revision's contract is loaded. They are absent on the inspected local main.

[PR 204](https://github.com/Exios66/llm-mailroom/pull/204), inspected head `719f3816ca08f3079531d56130dcdbc5159af573`, is based on the PR 203 branch and holds a more permissive field-less-window confidence merge. Its graph can be unchanged while confidence and archival decisions change. Its description requires real-mode evidence and positive paired-bootstrap improvement before adoption; a diagnostics visualization does not satisfy that gate.

The inspected heads have merge-base **`b11555f4e6d2d2037711fb6ddda5dd01ce6dc952`**. Directly comparing PR 203 head with PR 204 head yields 45 changed files, including changes present only in newer PR 203 commits. PR 204's own merge-base-to-head delta is three files (91 insertions, 5 deletions), centered on `src/langchain_agents/specialist_agents.py`, associated regression tests and an agent-base docstring. These are distinct comparisons; do not attribute the direct head-to-head diff to CR6 alone.

PR descriptions report limitations, including crash-recovery identity duplication, unverified PostgreSQL behavior, and missing real-model evidence. Import these as linked known findings, marked with their evidence revision. Do not describe them as locally reproduced failures.

[The-Mailroom PR 71](https://github.com/Exios66/The-Mailroom/pull/71), inspected head `ed73ca64c823c4be3aa79ee8d1eeaecc9a118731`, is the downstream companion for PR 203: its description covers review-tail graph edges, cause tokens, resolve fields and review UI/proxy behavior. It intentionally vendors the staged display contract while retaining the earlier producer code pin, with `matches_pin: false` provenance until a post-merge pin refresh. This native tool complements that work; it does not duplicate the downstream operator desk or require PR 71 to run. If downstream comparison is requested, distinguish a declared staged-contract preview from an unexplained runtime/code mismatch.

## 3. Repository-native architecture and delivery

The following table describes the target layout. D0a provides the package, inspection/check code and tests; app/API/static/frontend entries remain **proposed**. All paths are relative to `tools/pipeline-diagnostics/`:

| Proposed location | Responsibility |
| --- | --- |
| `frontend/` | Optional Vite + React + TypeScript application, lockfile, build scripts, local proxy |
| `backend/mailroom_diagnostics/` | Snapshot generation, instrumentation adapters, schemas, findings, replay protocol and CLI |
| `backend/mailroom_diagnostics/api.py` | FastAPI router and same-origin static screen mounting |
| `backend/mailroom_diagnostics/app.py` | Standalone diagnostics-only FastAPI app that never imports `api.main` |
| `backend/mailroom_diagnostics/static/` | Build destination for packaged React assets; generated, not hand-edited |
| `tests/` and `fixtures/` | Tool-specific contracts, lifecycle/API/bundle/replay tests and synthetic defect fixtures |
| `pyproject.toml` | Nested tool packaging, CLI entrypoint, declared LangChain core dependency and generated asset inclusion |
| `README.md`, `QUICKSTART.md`, `COMPATIBILITY.md`, `PLAN.md`, `HANDOFF.md`, `EVIDENCE.md` | Navigation, setup, revision support, full design, resumable tasks and verification contract |

Keep the Python API and CLI usable when the React assets are absent. The screen should report how to build the optional UI rather than break API startup. The root package's allowlist will not discover code under this tools folder: install/build the nested package explicitly and include its generated static assets in its own package-data configuration. D0 includes a named packaging task for this declaration and an isolated wheel-install check using resource lookup plus HTTP asset retrieval; an editable install alone is insufficient evidence. Build release/container assets during packaging, so users of a prepared artifact need no Node installation. Plain editable installs continue to work without a frontend build. Ignore `node_modules`, build products and local captures within the tool folder; commit the dependency lockfile.

The nested setuptools configuration must map the Python package root to `backend/` (for example, `package-dir = {"" = "backend"}`) and discover `mailroom_diagnostics*` there. Install the parent mailroom package first, as the tool imports its `graph` and `pipeline` modules; the nested package is not a replacement pipeline distribution.

The root exporter lives under `src/scripts`, which the parent wheel's package allowlist does not install. Do not make the nested tool import that checkout-only script in packaged mode. Factor the existing compiled-graph extraction into a small importable, side-effect-free pipeline helper used by both the exporter and the tool, and test parity. Diagnostic JSON output must be parseable on stdout with logs on stderr; do not assume incidental logging from the existing CLI is a machine-data protocol.

Development: the React dev server proxies diagnostic requests to this repository's API. Production/local packaged mode: FastAPI serves the built app under `/diagnostics` with a correct asset base and SPA fallback restricted to that mount. Register diagnostic APIs explicitly under `/v1/diagnostics`; the current legacy alias helper only handles an allowlist and will not register new endpoints automatically.

Normal API mode can observe the embedded watcher through a thin mount hook in existing `src/api/main.py`. The standalone diagnostics entrypoint must construct its own minimal app and **never import `api.main`**, which currently performs score/embedding warmups at module scope. Share existing token validation by extracting only the necessary side-effect-free auth helpers during implementation. The standalone path suppresses watcher startup, Gmail polling, status email, relations effects and trace-backend initialization before any pipeline import. Importing a snapshot or opening the workbench must never start document processing. This is the default for agents inspecting a checkout or a bundle.

Use React Flow (`@xyflow/react`) for custom nodes, edges, selection, minimap and keyboard support. Keep React state small: a query/cache layer for snapshots and a reducer for events/selection. Evaluate a layered layout engine such as ELK in a worker during the implementation spike; preserve cyclic retry edges explicitly rather than forcing the pipeline into a DAG. Pin compatible versions after the spike. Do not add a root Node toolchain or change the pipeline orchestrator.

## 4. Versioned graph and behavior snapshots

Retain the existing contract's meaning. Add optional enrichment through a compatible minor contract version, or a versioned diagnostics envelope referencing the unchanged contract. Coordinate any contract schema change with downstream consumers and their supported major versions.

Each snapshot should contain:

- Original graph nodes/edges, stable node IDs, and stable edge IDs based on source, route token, target and branch identity. Preserve multiple route tokens reaching the same target; do not collapse them by endpoints alone.
- Router callable binding and source reference for each conditional branch. Read actual builder registration metadata; where introspection cannot supply stable metadata, add a helper around registration that records and passes the same inputs to LangGraph. Parity tests compare helper output with compiled wiring.
- Descriptive roles and lifecycle attributes: procedural/LLM-assisted, interruptible, entry/terminal boundary, optional capability, graph versus external effect. An observation type of `agent` does not prove an LLM call: the reporter is procedural.
- Git SHA, dirty flag/content hashes, topology hash, routing/source hash, taxonomy hash, contract hash, effective-policy hash, feature flags and generation timestamp. Source references use repo-relative file/function identifiers; absolute workstation paths are excluded from shared bundles.
- An allowlisted effective configuration, including per-class thresholds and resolved model/prompt provenance where available. Never export credentials or the full environment. Configuration provenance must say whether it came from a running worker, a fresh preview process, or a file artifact.

A hash detects a change; it does not explain or prove its semantics. Display `topology changed`, `policy changed`, `implementation changed`, or `evidence changed`, with the relevant diff. Include upstream helpers affecting confidence/guards in implementation provenance so PR 204 is visible even when routing.py and edge lists do not change.

Provide separate `workspace preview` and `running worker` snapshots. A local file watcher can regenerate a preview in a fresh isolated process after relevant source/config edits. Debounce saves, atomically publish a fully validated snapshot, and retain the last valid one with a visible build error when edits are incomplete. **Do not hot-reload production graph code or clear live config caches.** A restart/redeployment makes a new worker snapshot active; old runs retain their original snapshot IDs.

The UI may compare two locally generated bundles or CI artifacts from exact commits. It must not execute an arbitrary remote PR checkout on demand. For attribution, compare each staged head with its recorded merge-base; label newer PR 203-only changes as absent from the inspected PR 204 base. To evaluate adopting CR6 onto the latest PR 203, prepare a trusted isolated candidate containing that latest head plus the held merge delta, and compare that candidate with the latest PR 203. Do not substitute the older raw PR 204 head for this adoption candidate.

Represent graph-external behavior as a distinct, collapsible context layer: watcher claim/admission → main graph or Gmail single-document triage → terminal disposition; terminal-triggered relations scans and completion echoes as asynchronous effects; heartbeat/watchdog as system health. Show the BERT/intake preparation work as substeps unless executable wiring actually promotes them to nodes. Model capability handoff from free triage to the full graph explicitly. Identify these boundary relationships through tested producer-owned declarations and observed events, not invented LangGraph edges. Exception/budget aborts in `_execute_run` are external lifecycle exits. Neither a triage archive nor a human-applied report-tail resume should be flagged for skipping ordinary extraction nodes.

## 5. Native execution events and live updates

Add diagnostic instrumentation around existing node execution and conditional routing. It is independent of the configured tracing sink and emits no LLM calls. Preserve Langfuse/Braintrust/Phoenix integrations; link to their observations when IDs exist.

The event envelope needs `schema_version`, `event_id`, `producer_id`, process epoch, `run_execution_id`, `doc_id`, optional logical-document/content identity, `node_id` (the exact LangGraph registration key), optional `span_name` (a potentially shared trace label), attempt, node invocation ID, sequence, snapshot ID, timestamp, event type, and curated payload. Routing overlays always use node IDs, never a span name to infer identity. A trace ID based on a filename is insufficient for unique execution identity or repeated runs. Clock time alone is insufficient for causal order.

Core events: run started/finished/aborted; node started/completed/errored/interrupted; node re-entered after resume; route selected; review parked/resolved; diagnostic transport gap. Keep per-node invocation identities through retries. LangGraph interrupt/resume is a control-flow event, not an error or evidence of a missing terminal edge.

Route events record the router result, selected edge, applicable effective thresholds, relevant post-guard confidence/counters/flags, and review causes. Capture raw confidence separately where the underlying agent exposes it. Do not fabricate a raw value from a post-guard value. Exact explanation codes should be emitted by the relevant routing branch; if only destination is known, label the explanation unavailable. Any annotation text is subordinate to executed decisions.

Do not serialize DocumentState wholesale: it contains document text, extraction values, potentially gold and private metadata. Use strict payload allowlists and size limits. Gold remains outside routing state for calibration, with oracle-assisted runs visibly separate. Findings can reference approved evidence without embedding source documents.

Use one backend event broker per process, bounded subscriber queues and a bounded resumable journal. Opt-in diagnostic persistence should be separate from the hash-chained business audit log. For standalone watcher processes, use a shared local journal/collector adapter rather than assuming API memory is shared. Support the existing database backend abstraction; start by verifying SQLite and explicitly scope PostgreSQL readiness. Failed diagnostic writes must not block processing; expose dropped-event counters and incomplete coverage.

Serve HTTP snapshots plus an SSE stream for this one-way diagnostic feed. Browser authentication uses an authenticated fetch stream with the existing bearer header, or a same-origin authenticated session; native EventSource cannot set an arbitrary Authorization header. Do not put long-lived bearer tokens into stream URLs. Add heartbeat, reconnect/backoff, bounded history and cursor-based resumption; expired cursors require a fresh snapshot and explicit gap marker. Polling is the fallback.

Bootstrap with a snapshot plus journal watermark, then replay subsequent events so events arriving during page load are not missed. Deduplicate by event ID; order by execution sequence and causal identity, not arrival time. Tag every event with its originating worker snapshot so multiple revisions can run concurrently without repainting old runs onto a new graph. Slow browser clients must never backpressure the pipeline.

Source status must distinguish **live**, **stale**, **disconnected**, **snapshot**, and **replay**. Offline bundles retain recorded observations with their original timestamps; they must never impersonate a live feed.

Three end-to-end diagnostic journeys define native usability. A developer edits wiring, sees a validated workspace diff, restarts a test worker, and sees that worker's new active snapshot while previous runs keep theirs. A tester runs a mock failure fixture, selects the actual retry/review edge, sees its inputs and cause, and prepares the redacted run evidence that the D4.2 bundle carries. An agent checks the same evidence through the CLI, receives stable finding IDs and incomplete-evidence status, and returns a fixture-backed proposed fix. None of these journeys requires another repository, hosted tracing or a real-model call.

## 6. What “find missed connections” can actually mean

Use a deterministic diagnostic engine with structured evidence. Separate a proven structural defect, a violated tested invariant, an untested branch, an observed anomaly, and a proposed behavior that lacks implementation. Zero observations do not prove an edge is dead; tests over finite fixtures do not prove all Python states safe.

| Check | Method and proof boundary | Result |
| --- | --- | --- |
| Orphan or unreachable node | Reachability from every declared entry, accounting for interrupts and enabled capabilities | Structural finding with node/edge evidence |
| No exit or uncontrolled cycle | Strongly connected components plus declared interrupt/retry/deadline exits; test counters at and beyond bounds | Potential cycle issue unless an unbounded path has a concrete witness |
| Router returns an unmapped token | Invoke actual routers on bounded scenario matrices; compare returned token with registered path map | Confirmed mismatch for the supplied witness state |
| Missing quality gate | Routing invariant tests: invalid/hollow/failed-window extraction cannot enter an automated archive tail; human-applied fields have explicit provenance and validation | Violation with minimal reproducible fixture, not a general graph-color heuristic |
| Review without a cause | Enumerate reachable review fixtures and assert cause tokens; reuse PR 203 cause-coverage tests | Coverage finding scoped to the tested state domain |
| Unobserved branch | Compare exact route events or replay requirements with scenario coverage | Evidence gap; never a fabricated pass or declared dead connection |
| Undeclared observed transition | Compare exact node/route events against that execution's snapshot | Runtime mismatch; with incomplete telemetry, report inconclusive |
| Missing producer/UI mapping | Contract schema validation and capabilities/mapping checks | Contract drift with missing IDs |
| Data handoff gap | Explicit producer/consumer state-field contracts and scenario tests; account for defaults, partial updates and human resume | Missing field/provenance witness, or incomplete contract coverage |
| Recovery/integrity anomaly | Link stress-harness results: duplicate document/catalog identities, orphan rows, broken chains, stolen claims | Harness finding with revision, arguments and artifact reference |
| Missing intended behavior | Versioned expectation/invariant linked to an issue and a failing fixture, or an explicit unimplemented design annotation | An intent gap, distinct from an existing runtime connection |

Add boundary scenarios for each class's low/high/judge-band thresholds (below/equal/above), unknown classes/schema, null confidence, invalid/hollow extraction, failed/empty windows, transient retry exhaustion, budget expiry, judge/arbiter/boss outcomes, both review-resume mechanisms and concurrent resolution. Prefer bounded equivalence partitions and pairwise combinations over blindly enumerating a huge Cartesian product. Test schema and realistic node preconditions before calling a state reachable.

A finding contains stable ID, rule/version, category, severity, proof status, snapshot IDs, affected node/edge IDs, witness fixture, evidence links, reproduction recipe, owner and optional issue link. Acknowledgement requires a reason and expires or revalidates when the affected implementation changes. Machine output distinguishes violation, unavailable and inconclusive.

Semantic omissions require an expectation to compare against. The tool cannot discover every intended behavior from the code alone. Optional agent analysis can suggest a missing scenario/connection, but remains a proposal until backed by an invariant or reproducible failure.

## 7. PR 203 and 204 diagnostics and calibration

Consume PR 203's RunRecord, policy_replay, calibrator, hillclimb and stress outputs through adapters; reuse the Dojo's metrics and the objective's guard definitions. Do not build alternative scoring or confidence formulas in the frontend.

Policy changes can be replayed from recorded attempts. PR 203's `policy_override` clears config caches and is explicitly not thread-safe; its replay environment also patches routing helpers. Run each replay job in a **fresh interpreter process**, created by subprocess exec/spawn, with an immutable input snapshot, temporary output directory, bounded resources and a fixed operation allowlist. Do not inherit a live worker through fork or reuse a process between policies/jobs. Context managers restoring patches are useful but do not replace the isolation boundary. Never run overrides inside the API or live watcher. Concurrent users get isolated jobs. The server must not accept arbitrary shell commands or unrestricted filesystem paths.

Show baseline/candidate routes, class-specific thresholds, raw/post-guard confidence when observed, attempt counts, guardrail causes, review outcomes, latency/cost provenance, class-level sample sizes, interval/sample shortfalls, train/test status and missing observations. Distinguish sweep-only records from actual executed paths. Current RunRecord has an actual-route slot; that is not evidence that production automatically populates every attempt or path.

Preserve `unobserved` replay outcomes for missing judge attempts, collapsed subclasses, classification arbiter, boss and other unsupported observations. “Full-information” does not mean every possible branch is recorded. Show oracle-assisted, synthetic/mock, unsplit and real held-out results separately. Calibration sliders submit a temporary policy to Python replay and never mutate taxonomy. Display existing G1–G4 and paired-bootstrap decisions rather than recalculating or weakening them.

PR 204 requires an additional evidence seam: current aggregate extraction records expose chunk counts and merged confidence, not every chunk's fields/confidence. Add optional versioned per-window diagnostic observations: window/index/offset coverage, success/failure, presence of canonical extracted fields, raw/validated confidence, and the aggregation strategy/version. Keep document contents and field values out of this record.

With complete compatible observations, recompute both merge strategies in trusted revision-specific worker code and compare resulting routes. Include productive + empty windows, all-empty windows, failed windows, malformed output, and a false-negative empty window. If observations are absent, say **cannot compare this merge change from aggregate records**; obtain a matching new collection instead of treating threshold-only replay as evidence. Recomputing a merged confidence is still insufficient for a real-quality claim without compatible gold and downstream observations.

Expose PR 204's held status and evidence requirements. Unit/synthetic fixtures validate the harness; the workbench cannot authorize or claim real-model experiments, threshold promotion, or deployment merely because the graph renders successfully.

## 8. Screen behavior, styling, accessibility and agent access

Visual direction: a restrained engineering workbench with clear type, consistent node cards, semantic phase groups, orthogonal connectors, unobtrusive grid and light/dark themes. Keep fixed node sizes and stable positions so frequency changes do not move targets. Use icons, line styles and text in addition to color. Animate only the selected live path, with pause and reduced-motion support.

Desktop: top provenance/source strip; mode and class/run selectors; graph canvas; collapsible findings rail; selected node/edge inspector; bottom event/replay timeline. Clicking a connection shows its router, source reference, observed decision evidence and relevant tests. Search, fit/reset, collapse phases and path-only filters must not conceal a selected finding. Persist layout by topology revision; preserve selection, zoom and keyboard focus on ordinary event updates.

Tablet: one side panel at a time. Mobile: graph/list toggle, default readable ordered path or connection list, expandable node details, and a bottom sheet for findings. Scrolling and keyboard navigation must work without pinch/drag gestures. A wide graph is not a reason for the entire page to overflow horizontally.

Target WCAG 2.2 AA: keyboard access to graph and list equivalents, visible focus, meaningful node/edge labels, contrast, large touch targets, no motion dependency and polite batched status announcements. Provide an accessible table/list exposing every connection, reason, state and finding. Test keyboard-only and screen-reader flows manually as well as automated checks. React Flow accessibility features are a starting point, not certification of the finished screen.

Agents get the same semantics through versioned JSON/OpenAPI and a CLI, not screenshot interpretation. Stable node/edge/finding IDs also appear as DOM attributes. Deep links encode snapshot/run/node/finding selection. Bundle export contains snapshot, redacted events, findings, scenario coverage, replay provenance and hashes so a failure can be reproduced without live services. Bound archive extraction/file sizes and validate schema/content types; bundles never contain executable commands.

Specify the CLI protocol during D0: `inspect`, `check`, `compare`, `replay`, `export` and `serve` subcommands, with `--json` output and explicit input artifact IDs/paths. Proposed check exit codes: 0 = complete and no findings at the configured severity, 1 = findings, 2 = invalid input/execution failure, 3 = incomplete evidence. Always include proof status and coverage in JSON. Offline bundle operations make no network calls; replay cannot dispatch live LLM calls. Document commands only after their real entrypoints exist.

Proposed API surface:

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/diagnostics/capabilities` | Available modes, permissions, versions, worker/source status |
| `GET /v1/diagnostics/snapshot` | Current preview or running-worker snapshot and stream watermark |
| `GET /v1/diagnostics/events` | Authenticated resumable SSE; bounded HTTP event reads as fallback |
| `GET /v1/diagnostics/runs/{execution_id}` | Exact run path and per-attempt evidence, with redaction |
| `GET /v1/diagnostics/findings` | Filterable findings with machine-readable proof status |
| `POST /v1/diagnostics/jobs` | Allowlisted check/compare/replay job using approved artifact IDs |
| `GET /v1/diagnostics/jobs/{job_id}` | Status, cancellation capability, result/artifact references |
| `DELETE /v1/diagnostics/jobs/{job_id}` | Cancel an isolated diagnostic job and release resources |

Start with capabilities/snapshot/findings, then add lifecycle and jobs. Reuse existing bearer validation and loopback policy. New diagnostic job endpoints need authentication, bounded concurrency, cancellation and timeouts even though they do not spend model tokens. Keep source text and mutation controls behind appropriate existing access checks. The screen does not expose credentials to agents or browsers through exported configuration.

## 9. Delivery sequence and acceptance gates

| Phase / proposed PR | Deliverable | Acceptance evidence |
| --- | --- | --- |
| D0: native shell and contracts | Nested package/assets, CI test-root/path mapping, mount, independent app, schema, snapshots, capabilities, CLI inspect | Watcher-free launch; asset and missing-asset modes; isolated wheel resource + HTTP check; planner includes nested tests in affected/full runs; no hosted/LLM calls |
| D1: graph + deterministic gaps | React graph/list, source inspector, structural checks and bounded route scenarios on current main | Add/remove a fixture node/edge in an isolated builder or disposable checkout: screen changes without frontend mapping edits; unmapped route has a witness; unchanged baseline passes contract check |
| D2: exact live runs | Node/router/lifecycle events, broker/journal, SSE + fallback, standalone watcher adapter | Mock run animates exact node IDs; interrupt/resume correct; reconnect/gaps visible; mixed revisions isolated; export-contract and mock pipeline gates pass (pilot fixture limitation disclosed) |
| D3: calibration and comparisons | PR 203 adapters, isolated jobs, base/head and policy diffs, evidence completeness | Replay matches existing harness; sequential and concurrent jobs each get fresh interpreters, leave live taxonomy unchanged; missing observations stay unobserved |
| D4: PR 204 merge evidence + delivery hardening | Per-window seam, compatible merge comparison, export/import, responsive/a11y/performance verification | Unchanged topology still flags merge implementation change; incompatible records rejected; realistic window fixture comparisons; accessible mobile and offline reproduction |

D0–D2 provide the useful native debugging MVP. D3 makes it a calibration companion; D4 supports the held merge investigation. Do not make D0–D2 depend on merging PR 204. PR 203 adapters target its eventual merged schema, retaining a useful topology-only mode on main. Keep this as its own reviewed PR sequence to avoid enlarging PRs 203/204 or duplicating their in-flight edits.

Current-main support must go beyond a diagram: D1 supplies fixtures for today's missing route/quality/hand-off coverage and source-linked findings (reproduction-bundle export is D4.2); D2 adds exact mock-run evidence. Add capability adapters rather than unconditional imports of RunRecord or policy_replay, which are absent on current main. Baseline mode must launch and check the current graph without either PR. PR 203 mode adds observed-record/replay features only when its actual schemas and helpers are compatible. PR 204 comparison additionally requires strategy-compatible window evidence. Detect these capabilities and schema versions, not PR numbers or branch names; missing advanced features disable only the affected operation.

Maintain a compatibility matrix across current main, PR 203's staged head, and the PR 203 + PR 204 candidate. Run contract/fixture conformance against immutable artifacts and separately prepared checkouts for each trusted revision, and record precise SHAs. The UI can compare their exported results without checking out branches in the user's active workspace. Refresh staged-head fixtures before merge and repeat after merge; do not assume the branch cannot change.

Planning estimate, to refine after D0: roughly 15–25 engineering days for one implementer plus review, split across the five phases. Cross-process telemetry, packaging and evidence compatibility are the largest uncertainties. Real-model collection and owner review are separate elapsed-time dependencies, not included in that estimate.

## 10. Verification, performance and rollout

For Python changes, run compileall, affected tests, and the full suite on the final reviewed state. Graph/trace/contract changes also need `export_contract.py --check`, the mock experiment regression gate, and the mock pilot when its fixtures are present. Record absent sample assets explicitly. Docs need `test_docs_truth.py`. Do not claim a configured Python linter/typechecker: neither exists. The optional UI needs its own locked dependency build/type validation and browser verification, scoped to this new package.

Every phase's gate includes affected tests for root Python changes and explicit collection of the nested tool's own tests, because explicit `src/tests` runs exclude the nested suite even though default pytest discovery now includes both roots. Include the nested backend in syntax verification; `compileall src` alone will not inspect it. LangChain core is already a root dependency; the nested package must declare its direct use too, while resolving a compatible version with the parent install. No graph construction, state inspection or mock scenario should require an actual chat completion.

D0a registers the nested test root and a `tools/pipeline-diagnostics/` input mapping in `src/scripts/ci_tests.py`, updates root pytest `testpaths`, and extends `src/tests/test_ci_tests.py` to prove both affected and full plans collect the nested tests. The previous planner's full profile ran only `src/tests`; an unrecognized tools change did not solve that. Nested tests do not automatically inherit `src/tests/conftest.py`, so their hermetic fixtures must independently prevent real LLM/network access. Keep eventual frontend build checks explicit in CircleCI as they are outside Python test discovery.

D1's mutation demonstration is fixture-only: it cannot leave test nodes in production wiring while claiming the committed contract is current. An intended real graph change instead regenerates the contract through the normal exporter workflow, then checks the new committed artifact. Record the mutation fixture and baseline gate as separate evidence.

Use meaningful diagnostic regressions: deliberate orphan node, unregistered route token, bounded versus exhausted retry, invalid extraction routed to report, review without cause, pause/resume, delayed/duplicate/out-of-order events, stale configuration, source loss, mixed worker revisions, corrupted/oversized bundle, replay timeout and unavailable branch. Use mock/synthetic data only in automated tests and public screenshots. Tests must show the injected defect is detected and the corrected fixture clears it.

Capture desktop and mobile screenshots and short interaction recordings under `.evidence/` according to repository policy. Validate 360px, 768px and desktop widths, keyboard focus during live updates, reduced motion, contrast, screen-reader equivalent and readable 200% zoom. Measure native build, initial load and installed-wheel behavior.

Performance targets are proposals to benchmark, not achieved results: selected local-run updates visible within 1 second p95 of event emission; topology preview published within 2 seconds of a valid edit; normal event updates preserve 60fps interaction where practical. Replay 100 concurrent synthetic runs and 50 events/second, then burst above capacity to verify bounded memory and explicit gaps. Compare mock pipeline duration and storage contention with diagnostics disabled/enabled; aim for under 5% median overhead on a fixed repeated workload and publish sample size/p95. If overhead or contention exceeds the agreed budget, reduce capture or use a separate collector before enabling broadly.

Roll out in stages: offline topology/bundle mode → opt-in local live capture → opt-in CI diagnostic artifacts → reviewed native packaged/container builds. CI belongs in CircleCI, not a new GitHub Actions workflow. New graph/behavior diffs and diagnostic findings can be attached to this repo's normal evidence contract; semantic proposal annotations do not become blocking CI rules until backed by approved invariants.

Cross-repo impact: no other repository is required to run this tool. Additive display-contract or trace metadata changes may still require downstream conformance updates in The-Mailroom and later sandbox/hub refreshes. The unified-audit-row work is another future consumer: keep diagnostic events separate from authoritative audit rows and use adapters rather than creating competing audit/scoring semantics.

PR 203's existing downstream catch-up is tracked by The-Mailroom PR 71, rather than being a new prerequisite created by this plan. Any additional diagnostic metadata or contract schema changes from D0–D4 need their own compatibility review; do not assume PR 71 already covers an interface that does not yet exist.

## 11. Decisions to resolve during D0

- Prefer packaged built assets for native usability; verify release/container mechanics and the checkout build fallback before promising an installation command.
- Choose journal retention and standalone-watcher transport from measured volume and available database deployments. Default capture must have a clear disk budget and expiry behavior.
- Establish the minimal versioned state-field/expectation declarations that enable useful handoff checks without pretending to infer every node's semantic requirements.
- Confirm the eventual PR 203 RunRecord/replay schemas and PR 204 baseline. Add adapters/migrations instead of fixing a moving branch's assumptions into the UI.

These are implementation choices with recommended defaults, not reasons to move the tool out of this repository or defer the native MVP.

## References

- [PR 203: calibration, routing, scale and review](https://github.com/Exios66/llm-mailroom/pull/203).
- [PR 204: held field-less-window confidence change](https://github.com/Exios66/llm-mailroom/pull/204).
- [The-Mailroom PR 71: downstream PR 203 adoption](https://github.com/Exios66/The-Mailroom/pull/71).
- [React Flow accessibility](https://reactflow.dev/learn/advanced-use/accessibility): keyboard, focus and screen-reader affordances to build on.
- [MDN server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events): one-way transport, event IDs, reconnect and heartbeat format.

This design is not evidence that its remaining screen, instrumentation, calibration or performance targets work. Only the implemented checkpoint's commit-specific raw results support its narrower claims. Preparing the draft PR does not authorize real-model pipeline runs, hosted syncs or policy promotion.
