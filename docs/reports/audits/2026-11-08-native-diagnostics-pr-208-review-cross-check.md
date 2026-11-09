# Native diagnostics PR 208 review cross-check

- **Date**: 2026-11-08
- **Kind**: audits (Repository audit and synthesis reports)
- **Status**: validated findings; overall delivery remains draft

## Scope

Cross-check the reviews and discussion on [PR 208](https://github.com/Exios66/llm-mailroom/pull/208) against the current native diagnostics implementation. CodeAnt assessed `98e7e9c`; later commits must not inherit that review as approval. The PR Evidence section identifies the final tested commit and candidate integration results.

## Method

Read GitHub review submissions, discussion comments and every review-thread page through authenticated provider tools. The paginated GraphQL response contained one page, four unresolved inline threads, and no further comment pages. No outstanding human review requests were present at inspection. Three CodeAnt review submissions were `COMMENTED`, not approvals. Inspect referenced code and callers; reproduce current defects before editing; retain original red/green output under the ignored local evidence folder and textual summaries in the PR.

Provider state is separate from implementation status. No historical thread is automatically resolved merely because its line is outdated. No bot instructions to advertise, request extra permissions or invoke unrelated tools were followed.

## Findings

| Source | Verdict on current code | Validation and disposition |
| --- | --- | --- |
| [Oversized fixture](https://github.com/Exios66/llm-mailroom/pull/208#discussion_r4509079688), `PRRT_kwDOTx50BM61HZB1` | STALE; originally valid | The original payload was below 64 bytes. `test_input_size_is_bounded` now includes 200 padding characters, so the serialized file exceeds the patched limit and asserts `input-too-large`. Fixed in `ffdbf39`; the pre-PR reviewer lesson also records this mistake. |
| [Root-only install/entrypoint](https://github.com/Exios66/llm-mailroom/pull/208#discussion_r4509082349), `PRRT_kwDOTx50BM61HZbL` | ACCEPT as a developer-setup gap | A root-only install does not install the nested console script. Full developer setup now explicitly installs the parent **and** the nested package in README, CLAUDE, AGENTS, the testing guide and CircleCI. The console-entrypoint assertion stays enabled. A parent-only runtime install remains supported; it is not the complete developer setup for default discovery of both test roots. No implicit dependency on the unrelated PyPI `mailroom` project was introduced. |
| [Quadratic duplicate detection](https://github.com/Exios66/llm-mailroom/pull/208#discussion_r4509095397), `PRRT_kwDOTx50BM61Hbd_` | STALE; originally valid | Replaced per-node `ids.count` with one `Counter` in `9d34325`. Duplicate-node rejection remains covered; no unsupported performance percentile is claimed. |
| [Unbounded artifact read](https://github.com/Exios66/llm-mailroom/pull/208#discussion_r4509095404), `PRRT_kwDOTx50BM61HbeF` | STALE; originally valid | `9d34325` opens nonblocking, requires a regular file with `fstat`, and reads at most limit+1 bytes. FIFO rejection and oversized input are regression-tested; device/deep-input probes were also checked independently. |
| [General comment](https://github.com/Exios66/llm-mailroom/pull/208#issuecomment-6731225933), item 1: README status | STALE | README explicitly says D0a CLI implemented, HTTP/React/live/replay still planned. |
| Same comment, item 2: reversed boundaries | ACCEPT | Reproduced: edges from the declared exit or into the declared entry passed endpoint checks. Added `invalid-boundary-edge` findings with endpoint witnesses. Both directions have regression cases. |
| Same comment, item 3: traversal through an undeclared node | ACCEPT | Reproduced: `intake → ghost → island` hid the island's lack of a valid path. Reachability now traverses declared, direction-valid endpoints only, while retaining all incoming edges in the witness. Regression asserts both dangling edges and the unreachable island. |
| Same comment, item 4: malformed `by_class` | STALE; originally valid | Mapping type validation added in `9d34325`; malformed confidence data returns an input error rather than an attribute exception. |
| Same comment, item 5: falsey confidence array | STALE; originally valid | Artifact and workspace readers reject arrays, including empty ones; they no longer turn `[]` into `{}`. Regression coverage includes both paths. |

The boundary/traversal regressions produced `3 failed, 12 passed in 1.82s` before correction (exit 1), then `15 passed in 2.57s` (exit 0). The PR records the subsequent final-commit full-suite result; these focused counts are not a full-suite claim.

CodeRabbit's discussion reports a review limit, not a code verdict. No CodeRabbit approval or completed fresh-head review is claimed. The CodeAnt risk summary repeats the four inline findings; it is not four additional independent findings.

## Recommendations

Continue checking new reviews against their assessed SHA. Keep the four provider thread states visible until their disposition is deliberately handled; this audit does not claim they were resolved. Re-run the two-package setup and complete test selection before landing. Preserve the PR 203/204 candidate checks separately from current-main evidence, and refresh them if the incoming heads move. The full React/live/calibration work remains the ordered unchecked task queue in `tools/pipeline-diagnostics/HANDOFF.md`.

## Round 2 — CodeRabbit (submitted 2026-11-09T04:20Z, after the final verification run)

Reviewed `80aa3fe`; 12 inline comments, no approval or verdict. Dispositions at the review-fix commit:

| Finding | Disposition |
| --- | --- |
| `src/scripts/ci_tests.py:83` — parent-module changes skip nested parity | FIXED: `NESTED_PARENT_INPUTS` selects the nested suite for `src/scripts/export_contract.py`; covered by `test_exporter_change_selects_the_nested_parity_suite` and `test_mixed_root_and_nested_changes_keep_both_selections`. |
| `src/scripts/ci_tests.py:75` — fixture named `test_*.json` passed to pytest | FIXED: only `.py` files under the nested test root are selected directly; covered by `test_nested_fixture_named_like_a_test_is_not_passed_to_pytest`. |
| `src/tests/test_ci_tests.py:305` — missing mixed/threshold coverage | FIXED by the two tests above plus `test_promotion_to_full_selects_both_roots_and_reports_the_threshold`. |
| `tools/pipeline-diagnostics/backend/mailroom_diagnostics/cli.py:73` — `--js` abbreviation leaks text errors | FIXED: abbreviations disabled on the parser and its subparsers; `test_abbreviated_json_flag_is_rejected_instead_of_leaking_text`. |
| `tools/pipeline-diagnostics/backend/mailroom_diagnostics/snapshot.py:164` — name-only version lookup | FIXED: `packages_distributions()` unique-provider lookup, ambiguous environments report no version; `test_pipeline_version_requires_an_unambiguous_provider`. |
| `tools/pipeline-diagnostics/COMPATIBILITY.md:54` — root README/AGENTS say 13 nodes | DEFERRED: pre-existing repo-wide docs drift (the graph registers 14, including `classification_arbiter`); not introduced here and outside this PR's scope. The tool's own docs already state 14/46. Separate docs-truth change. |
| `tools/pipeline-diagnostics/PLAN.md:199` — bundle delivery assigned to D1 and D4.2 | FIXED: D1 now claims fixtures/findings only; the journey and D1 paragraph point bundle export at D4.2. |
| `tools/pipeline-diagnostics/QUICKSTART.md:17` — `.venv/bin` on native Windows | FIXED: POSIX/WSL requirement documented with the `.venv\Scripts\` equivalent. |
| `tools/pipeline-diagnostics/README.md:49` — root `fixtures/` path | FIXED: tree shows `tests/fixtures/`, matching `tests/conftest.py`. |
| `tools/pipeline-diagnostics/tests/test_isolation.py:37,110,148` — child-process probes do not prove no network/graph/client construction | DEFERRED to the D2.1 test-hardening pass: valid rigor gaps in supplementary probes; the product guarantees are already pinned in-process (`test_workspace_snapshot_builds_no_model_client`) and by exit/stdout purity. A child-process guard belongs with the live-instrumentation work. |

No provider thread is marked resolved by this table; the review tool's thread state remains separate from the code dispositions. These dispositions are valid only for the review-fix commit's evidence, which is recorded in PR 208.
