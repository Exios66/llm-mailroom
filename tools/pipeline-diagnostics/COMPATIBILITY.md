# Compatibility and current-pipeline support

The native workbench must diagnose **today's code**, then add capabilities as staged work lands. It must not require unmerged code to launch or pretend unsupported evidence is available.

This document describes the target compatibility contract. The workbench itself is not implemented yet. Existing current-main exporter and test commands are in [QUICKSTART.md](QUICKSTART.md).

## Revision matrix

| Revision under diagnosis | Existing inputs | Native tool support to implement | Honest limitation |
| --- | --- | --- | --- |
| Current inspected main, `35ca9a4` | Compiled graph/exporter, taxonomy, routing functions, existing mock fixtures and audit/status APIs | Topology/source inspection, isolated router scenarios, gap witnesses and bundles; exact events after D2 hooks | No assumption that RunRecord, policy replay or new human-applied report-tail routes exist |
| PR 203 staged head, inspected `d18851fa` | Adds RunRecord, replay/calibration/hillclimb/stress outputs and review-tail paths | Adapt actual schemas, show new connections, consume evidence and run isolated policy comparisons | Some branches remain unobserved; staged schemas can change before merge |
| PR 203 plus PR 204 candidate, inspected PR 204 head `719f3816` | Changes field-less-window confidence merge; topology can stay identical | Implementation/merge comparison using compatible per-window observations and trusted revision-specific code | Aggregate confidence records cannot reconstruct missing window evidence; real-quality acceptance remains gated |

SHAs identify inspected inputs, not a permanent promise about branch contents. Refresh exact revisions and capabilities before generating evidence. PR 204 is stacked on the PR 203 branch; show any additional baseline drift rather than attributing all differences to the merge change.

At inspection, the common base is `b11555f4e6d2d2037711fb6ddda5dd01ce6dc952`. The direct PR 203-head → PR 204-head diff spans 30 files; the common-base → PR 204-head delta spans three. Compare each head to the common base for attribution, and mark later PR 203 changes as absent from PR 204's base. An adoption experiment against latest PR 203 requires a separately prepared candidate containing latest PR 203 plus the held CR6 delta; the raw older PR 204 head is not equivalent.

The downstream visualizer's [PR 71](https://github.com/Exios66/The-Mailroom/pull/71) already addresses PR 203 adoption. Its inspected head is `ed73ca64`, and its documented staged contract deliberately differs from its retained producer code pin (`matches_pin: false`). Display that provenance as an intentional preview when comparing artifacts; do not silently treat it as the running producer's revision or infer a broken connection solely from the pin difference. This repository-native tool has no runtime dependency on that PR or repository.

## Capability negotiation

Detect exported schema versions and available functions/features, not a branch name or PR number. The display contract version is `1.0.0` on the inspected main and both staged heads, so use content/topology hashes and feature probes to distinguish them; schema version alone cannot do so. Lazy-load staged adapters so absent imports do not break current-main graph inspection. Return machine-readable capability reasons for unsupported operations. Unknown schema versions fail the affected operation visibly while preserving baseline inspection.

Track these inputs separately: runtime worker revision, workspace preview, loaded policy, source configuration, observation schema, dataset revision/split, real/mock/synthetic mode and gold-in-state status. Never combine revisions silently or substitute current thresholds for a historical execution's effective policy.

## Debugging holes that already exist

Use current routing functions in isolated, bounded scenarios. Start with unmapped route tokens, absent review causes, guard ordering, unknown schema behavior, retry/budget boundaries and state handoff expectations. A concrete failing witness establishes a fixture-specific defect. A missing observation establishes an evidence gap. Zero visits alone establishes neither unreachable code nor a missing connection.

Reported PR 203 fixes and known crash-recovery limitations can seed regression scenarios on current main, but they must be reproduced before they are presented as confirmed local findings. Preserve differences in expected outcomes across revisions; do not rewrite baseline fixtures merely to make every version green.

Missing intended behavior requires a declared expectation, issue or failing scenario. Keep proposed connections separate from the executable graph until a reviewed code change actually implements them.

## Verification matrix to build

- Baseline launch and router diagnostics work with staged modules absent.
- Baseline and PR 203 snapshots preserve their different entry/review edges.
- Replay jobs receive fresh interpreter processes and cannot change a live worker's cached configuration.
- Shared trace labels do not merge distinct classify/retry/reviewer node IDs.
- PR 204 can trigger an implementation diff with zero topology changes.
- Missing per-window, judge, arbiter or subclass evidence remains unavailable/unobserved.
- Interrupted review, human-applied resume and Gmail triage do not generate false missing-extraction findings.
- Offline imported bundles reproduce the same finding IDs and clearly retain snapshot/replay status.

Generate and validate trusted revision artifacts in separate prepared checkouts. Import their data into the workbench; do not automatically execute arbitrary PR code or switch the user's active checkout. Refresh the matrix against merged main when staged work lands.
