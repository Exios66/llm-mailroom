## Problem
<!-- State the concrete failure/trigger and user impact in 1–3 sentences. Link the issue. -->

## Proposed solution and methods
<!-- Explain the resulting behavior, the approach used, and material tradeoffs. -->

## Blast radius
<!-- List affected nodes/edges, consumers, data/config/schema/prompts, and deployment surfaces.
     State whether existing files, manifests, catalog records or checkpoints are affected. -->

## Estimated human effort
<!-- Reviewer: N minutes. Operator/manual verification: N minutes. Migration/recovery: N minutes or none.
     Name required expertise and any action needed from another person. -->

## How to test
<!-- Copy-paste commands, prerequisites, fixtures and expected assertions. Include actual results.
     Explain which regression fails before this change. State what remains untested. -->

## How to verify
<!-- Give observable acceptance criteria: node sequence, handoff values, terminal artifacts,
     catalog/audit evidence. Distinguish mocked verification from any live run. -->

## Rollback
<!-- Exact revert/config procedure, process restart if needed, data/checkpoint compatibility,
     and handling for documents already processed. Do not recommend deleting audit history. -->

## Review evidence
<!-- Summarize independent review findings, fixes, and unresolved risks. -->

- [ ] Tests assert behavior and node handoffs, including the relevant failure path.
- [ ] Existing overlapping tests were extended/replaced where appropriate; removed coverage is explained.
- [ ] Test results and limitations are recorded above.
- [ ] Blast radius, human effort, verification and rollback are concrete.
