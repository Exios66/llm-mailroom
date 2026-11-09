# Evidence contract and post-hoc verification

This supplements `.claude/rules/evidence-contract.md`; it does not weaken it. It applies to each task in [HANDOFF.md](HANDOFF.md). The draft PR is the index linking implementation commits, raw results and artifacts. A green command proves only its stated coverage.

## An evidence record must identify its inputs

For every run, retain:

- Task ID, UTC start/end, full commit SHA and tree SHA, branch, comparison base and whether the tree was clean. Never label dirty-tree output as final-commit proof.
- Exact command, working-directory role, environment **allowlist** and dependency versions. Record relevant flag values, not `.env` or the full process environment.
- Exit code and original stdout/stderr. Include failure/skip summaries without editing numbers or hiding failures. A retry is a new run, not a replacement for the failed attempt.
- Input hashes: contract/snapshot, effective policy, implementation, fixture/corpus revision and split as applicable. Record mock/synthetic/real/oracle mode explicitly.
- Artifact locations, byte sizes and SHA-256 digests. Reference an immutable commit/CI artifact or the repository's evidence branch. A temporary workstation path alone is not a durable PR artifact.
- Coverage, known gaps and reviewer task IDs/verdicts. Test names and witness fixtures must be sufficient for a fresh agent to reproduce a finding.

Do not embed private document contents, environment secrets, client names, absolute workstation paths or unredacted dumps. Share synthetic fixtures and allowlisted metadata only.

## Required proof by milestone

| Claim | Evidence needed |
| --- | --- |
| D0a package/CLI works | Install command, dependency versions, installed import path; real `inspect` stdout parsed as one JSON object; check output and exit; focused test summary |
| Snapshot represents executable wiring | Byte-equivalent graph section against original exporter at the same revision; explicit preview/artifact mode and provenance |
| An edge change is discovered | Fixture-only old/new graph, resulting added/removed connection IDs and inspector/check behavior; unchanged baseline contract gate separately |
| A structural gap is detected | Minimal malformed/unreachable fixture, finding ID/witness, exit 1, corrected fixture exit 0; scope restricted to the implemented rules |
| Tests run in CI | Planner fixtures and actual CI selection covering nested tests for both affected and full runs; no assumed inheritance of root mock fixtures |
| Import/inspection is inert | Assertions against API/watcher/score/model initialization plus isolated smoke with tracing disabled; no document processing or live model requests |
| UI is usable | Desktop/mobile before/after captures and short interaction recording; keyboard, focus, reduced motion, screen-reader/list equivalent and relevant frontend build output |
| Live execution is accurate | Mock execution's exact IDs/attempts/route decisions compared with captured events; disconnect/gap/order/restart fixtures; collection of unavailable data remains explicit |
| Replay is isolated | Concurrent and sequential job tests, immutable policy inputs, fresh interpreter identities and unchanged live worker config; missing observations stay unobserved |
| A policy/merge change improves quality | Existing objective/guard outputs on compatible real held-out evidence, provenance and paired comparison; synthetic/mock evidence does not qualify |
| Installed build works | Isolated wheel installation and packaged resource/API smoke; editable checkout imports alone do not prove distribution usability |
| Performance meets a target | Method, fixed inputs, environment, sample count, baseline/candidate distributions and overhead; no fabricated p95 or cost figures |

## Current checkpoint versus feature completion

D0a may establish a native CLI foundation while the PR remains draft. It cannot claim an implemented React screen, active worker telemetry, full routing-predicate coverage, live code reload, replay, calibration or bundle protocol. Capabilities and quick starts must reflect what exists now.

An empty structural findings list means only that the supplied graph passed the implemented structural checks. It is not evidence that all routes are reachable in real traffic, every guard is correct, every intended connection exists, or calibration is sound. Unknown/unobserved results never become passes to make a dashboard green.

The original planning commit `133e971` and its 99 targeted checks are historical groundwork, not evidence for later implementation commits. Every later checkpoint needs its own clean-commit output.

## Command families to verify

Use the quick start for the **currently implemented** command spelling. Planned commands in the design do not count as executed evidence. The following repository gates already exist; run those applicable to the final change:

```bash
git status --short
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
.venv/bin/python -m compileall -q src tools/pipeline-diagnostics/backend
PYTHONPATH=src .venv/bin/python src/scripts/affected_tests.py --run -- -q
.venv/bin/python -m pytest src/tests/test_contract_export.py src/tests/test_ci_tests.py tools/pipeline-diagnostics/tests -q
PYTHONPATH=src OBSERVABILITY_PROVIDER=none .venv/bin/python src/scripts/export_contract.py --check
.venv/bin/python -m pytest src/tests/test_docs_truth.py -q
PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py --check
```

These are the prescribed evidence commands; the PR must state which were actually run and their exact results. The raw affected selector covers `src/tests` only; pair it with the nested suite, or use `ci_tests.py` for both roots. An affected selection is not the full suite. Exit 5 from the standalone affected selector means no tests selected, not a completed full run. After nested test discovery is implemented, the explicit complete Python selection is `src/tests` plus `tools/pipeline-diagnostics/tests`.

Graph/agent/routing/prompt changes also require the mock experiment gate and the pilot when its fixture manifest exists. Before marking ready, run the full applicable suites on the final commit and disclose fixture/environment skips. The mock experiment must use an isolated temporary MAILROOM_BASE_DIR; never overwrite runtime bins or re-approve the baseline as a shortcut.

Python has no configured linter/typechecker. The optional frontend's eventual build/type checks are separate; do not describe compileall as type validation. No real-model runs or hosted syncs are authorized by this evidence protocol.

## Publishing and preserving evidence

1. Commit the last source/doc edit locally and establish a clean tree.
2. Capture raw gate output under `.evidence/pipeline-diagnostics/<run-id>/` or CircleCI's existing test-results artifacts. Record the run's tested SHA outside the source tree to avoid a self-referential commit.
3. Put textual summaries, exit codes and relevant raw output in the PR Evidence section. Preserve failures and explain each material skip.
4. Publish synthetic screenshots/recordings to the repository's `pr-evidence` branch using the existing policy; link immutable artifacts in the PR. Keep binaries out of the feature branch.
5. Update the PR's handoff section with completed task IDs and evidence locations. A task is only checked after its acceptance evidence exists.
6. If source or docs change afterward, rerun the affected gates on the new final commit. Keep prior evidence labeled with its older SHA.

## Independent post-hoc audit

An auditor should be able to check out the PR's **tested SHA**, install the declared root and nested packages using pip, reproduce the commands, verify input/artifact digests and reach the same scoped verdict without the original agent's memory or local files. Compare expected failures on synthetic fixtures as well as passing cases. A later dependency resolution may differ: preserve the observed environment's package-version manifest as a CI artifact or PR attachment alongside the package declarations.

If an artifact has expired or cannot be fetched, mark its claim unverifiable until regenerated. Do not infer that a missing artifact proves either success or failure. If an agent stops mid-task, use the stop/resume record in HANDOFF.md and leave unmet checks unchecked.
