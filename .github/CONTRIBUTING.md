# Pull requests

Keep PRs centered on one concrete behavior change. For pipeline changes, select
the [pipeline PR template](PULL_REQUEST_TEMPLATE/pipeline.md) (add
`?template=pipeline.md` to the GitHub new-PR URL). This named template coexists
with the repository's default template and agent evidence workflow.
Replace its hints with evidence. Describe the failure, proposed behavior and methods
concisely enough that someone outside the conversation can review the change.

Name every affected node and downstream consumer in the blast radius. Include
configuration, dependencies, prompts, schemas, persistent records and checkpoint
compatibility when relevant. Estimate human effort separately for code review,
manual verification and operations/recovery; include assumptions behind estimates.

Before asking for review, run the relevant tests and record commands, outcomes and
limitations. Include how to verify the result and an actionable rollback procedure.
A green mocked test is evidence for orchestration, not model accuracy. Live calls,
provider spending and external notifications require an intentional test plan.

## Pipeline regression tests

Prefer extending an existing behavioral contract over adding another issue-specific
smoke file. Put compiled-graph handoff tests in `src/tests/test_graph_contracts.py`,
normalization cases in `src/tests/test_intake.py`, and isolated routing boundary
cases in `src/tests/test_routing.py`. Retain focused unit tests for behavior these
integration tests do not exercise. Remove a test only after identifying which
remaining test protects its useful behavior.

For a graph change, run the compiled production graph with scripted model responses.
Keep real state reducers, routing, guards, reporting and local persistence. Use a
temporary base directory and SQLite database; disable external services. Assert:

- The exact node sequence for each scenario; do not accept several unrelated routes.
- What the next agent receives: full prepared text, pages, class/subclass, prior,
  retry feedback and the relevant confidence/attempt values.
- What reaches reports, manifests and the catalog, including corrected retry output.
- Failure containment: invalid or unreadable content parks for review without report
  or archive, and transient errors preserve the appropriate retry budget.
- Original bytes and material facts survive preparation and archival.

Use complete expected outputs for deterministic cleanup. Cover document punctuation,
clause numbering, dates, amounts, links, tables, and long input; do not equate
"clean" with deleting every non-alphanumeric character. Never truncate documents.

Demonstrate test sensitivity by temporarily disabling the changed behavior or
injecting a representative handoff defect and confirming the relevant test fails.
Restore the implementation and rerun the tests. Record this evidence in the PR.
Do not add tests that only inspect source strings, assert mocks were called without
checking their inputs, or duplicate the implementation's branching logic.

```bash
python -m pip install -e '.[dev]'
python -m pytest src/tests/test_intake.py src/tests/test_intake_agent.py src/tests/test_graph_contracts.py src/tests/test_routing.py -q
python -m pytest src/tests -q
```

CI runs the full suite and the hermetic experiment gate in `.circleci/config.yml`.
There is no configured formatter, linter or typechecker; do not invent a new gate
inside an unrelated PR. See `AGENTS.md` for architecture and applicable project skills.

## Verification and rollback

Use temporary fixtures for manual verification. Record the expected terminal stage,
manifest fields and catalog/audit observations. For cleanup changes, compare the text
received by the sorter with the original file and confirm facts remain intact.

State the exact commit/config to revert and which watcher/API workers to restart.
Explain whether reverting requires a migration (and why), what happens to in-flight
checkpoints, and whether previously processed documents need an explicit reprocess.
Preserve source files and audit history; rollback does not undo past classifications.
