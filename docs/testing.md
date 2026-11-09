# Testing guide

CircleCI is the CI hub. The default job uses a conservative cascade so a small
change does not pay for every notebook kernel, packaging build and unrelated
test. The existing `affected_tests.py` import analyzer supplies the selection;
`ci_tests.py` adds mandatory contracts and full-suite fallback rules.

## Local development

```bash
python -m pip install -e '.[dev]'
python -m pip install -e tools/pipeline-diagnostics
# Inspect the plan for a branch, including uncommitted and untracked inputs:
PYTHONPATH=src python src/scripts/ci_tests.py --base origin/main
# Run the plan:
PYTHONPATH=src python src/scripts/ci_tests.py --base origin/main --run
# During a small edit, select explicit changed inputs:
PYTHONPATH=src python src/scripts/ci_tests.py --files src/agents/intake.py --run
# Explicit full validation, branch coverage, timing evidence and experiment:
PYTHONPATH=src python src/scripts/ci_tests.py --full --run
```

`--files` is an iteration aid: include every changed input. CI always derives
inputs from Git. To test a single function directly, use ordinary pytest:
`python -m pytest src/tests/test_routing.py -q -k classify`.

The full Python selection includes `src/tests` and `tools/pipeline-diagnostics/tests`.
The raw `affected_tests.py` helper scans only `src`; pair it with the nested suite
or use `ci_tests.py` for combined selection. Install the nested package after the
parent. Its tests carry their own hermetic fixtures.

## Cascade and escalation

| Scope | What runs |
| --- | --- |
| Every scoped run | Routing, output guards, graph-node contracts and compiled pipeline E2E smoke tests. The stronger `test_graph_contracts.py` suite joins automatically when present. |
| Nested diagnostics change | Smoke plus explicitly mapped `tools/pipeline-diagnostics/tests`; nested docs also retain root documentation checks. |
| Small mapped change | Smoke, then all transitively affected test files (including literal dynamic imports/monkeypatch targets). No smoke file is run twice. Runtime/script changes also run the hermetic experiment baseline. |
| Markdown/reStructuredText | Smoke, documentation consistency checks, and any tests that reference changed filenames. |
| Shared/high-risk or uncertain change | Full pytest suite with branch coverage, then the hermetic experiment baseline. |
| Release branches, manual override, weekly backstop | Full validation, including notebooks and packaging. |

Full escalation includes graph, schema, storage, LLM/provider/prompt, tracing,
taxonomy, dependency, deployment, vendored code and shared-fixture changes;
shared agent/sorter bases, LangChain integration, bins/environment/configuration
and package initializers;
CI/selector configuration; any `conftest.py`; deleted/renamed inputs; unknown
file types; invalid Python; analyzer/ref failures; production files without a
known test consumer; eight or more changed production files; or an affected
area covering at least 65% of test files. The JSON plan states the reason.
These are deliberately conservative initial boundaries, not size-based proof
that a change is safe. New/dynamic dependencies may evade static analysis;
mandatory graph contracts and periodic full runs are the backstops.

Failures stop the cascade and fail CI. A later successful command cannot hide
an earlier failure, including pytest's no-tests-collected exit. No tests are
silently skipped because selection is unavailable: uncertainty expands scope.
The fast path does not deselect individual tests inside an affected file.
The lower-level `affected_tests.py --run` exits 5 on an empty selection; use
`ci_tests.py` for automatic full-suite fallback instead of treating that as a pass.

## CircleCI and free-tier usage

`.circleci/config.yml` uses one `small` executor with no paid service, parallel
job fan-out, or automatic rerun loop. PRs compare with `origin/main`; builds of
`main` compare with the last successfully validated revision restored from the
branch baseline cache. This includes earlier commits in a multi-commit push
and changes from failed builds. A missing/non-ancestor baseline forces full
validation to bootstrap a new safe baseline. Changes to the policy itself run full validation.

Successful runs are cached by both source-tree hash and planned test scope.
An affected-only pass cannot satisfy a full run, and a failed run never writes
a green marker. Pip's dependency cache is retained. Wheel-cache warming runs
only when the wheel tests or full suite are selected.

A full backstop is scheduled once a week, Sunday at 08:17 UTC, and always
bypasses the green-result cache. The pipeline parameter `full_suite: true`
forces the same validation when manually triggering CircleCI. Legacy scheduled
workflows require GitHub OAuth integration; for GitHub App integration, replace
the scheduled workflow
with a CircleCI schedule trigger passing that parameter, rather than adding a
second scheduler. See [CircleCI's schedule support](https://circleci.com/docs/reference/configuration-reference/#schedule).

The existing required check remains `ci/circleci: test`. Conservative scope
rules and the weekly cadence can be adjusted after reviewing real duration and
coverage artifacts; do not narrow them merely to get a green build.

## Coverage and evidence

CircleCI publishes `test-results/`:

- `test-plan.json`: changed paths, selection reason, test files and profile.
- `execution.json`: stage duration and exit status.
- Stage JUnit XML and the 20 slowest pytest durations in job output.
- `coverage.xml` on full runs: line and branch coverage of `src`, excluding
  tests. Per-test execution contexts are retained in the local `.coverage`
  database when running the full command.

Full runs include the expensive headless notebook re-execution from both
working directories; these remain intact. Small changes avoid unrelated
notebook and wheel tests instead of weakening their assertions.

Coverage is a diagnostic, not proof of correct behavior. There is no invented
repository-wide percentage threshold before a baseline is measured. Use
uncovered branches to add failure-path and boundary assertions, especially at
node handoffs. Test counts alone are not evidence of meaningful coverage.

## Writing useful tests

Extend existing behavior suites before creating another issue-specific file.
Use `temp_base_dir`, mocked model/provider fixtures and real local graph,
routing, guards and persistence where those are the behavior under test.
Keep all tests free of real model calls and external notifications.

For deterministic preparation, assert complete expected text and preservation
of facts. For graph changes, assert the exact route, what the next node receives,
retry budgets, failure containment, and what reaches manifests/catalog/archive.
Demonstrate regression sensitivity by injecting the relevant defect and seeing
the assertion fail. Keep original bytes and audit history intact.

Tests should be independent of order. The audit conflict investigation found
that enabling cached production loggers before `structlog.testing.capture_logs`
can make log-event assertions fail in selected orders. Give such tests isolated
logger state; do not remove event assertions or change production behavior just
to make a selected order pass. A selector cannot repair test isolation.
