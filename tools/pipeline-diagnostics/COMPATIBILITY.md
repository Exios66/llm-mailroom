# Compatibility and current-pipeline support

The native workbench must diagnose **today's code**, then add capabilities as staged work lands. It must not require unmerged code to launch or pretend unsupported evidence is available.

This document is the **historical compatibility target** recorded for the staged work; the revision matrix below describes inspected heads, not the current tree. For what runs today, use [QUICKSTART.md](QUICKSTART.md).

**Current state (2026-11-09):** PR 203 and PR 215 are merged on `main` (`1b9477c`); this diagnostics branch is merged up to that revision. PR 204 remains held and is **not** part of the current tree. Do not describe the older inspected heads below as current.

## Revision matrix

| Revision under diagnosis | Existing inputs | Native tool support to implement | Honest limitation |
| --- | --- | --- | --- |
| Historical inspected main, `35ca9a4` (not current) | Compiled graph/exporter, taxonomy, routing functions, existing mock fixtures and audit/status APIs | Topology/source inspection, isolated router scenarios, gap witnesses and bundles; exact events after D2 hooks | No assumption that RunRecord, policy replay or new human-applied report-tail routes exist |
| PR 203 staged head at inspection, `bee18cb0` (now merged as `main` `1b9477c`; SHA kept for evidence history) | Adds RunRecord, replay/calibration/hillclimb/stress outputs and review-tail paths | Adapt actual schemas, show new connections, consume evidence and run isolated policy comparisons | Some branches remain unobserved; staged schemas can change before merge |
| PR 203 plus PR 204 candidate, inspected PR 204 head `719f3816` (PR 204 still held) | Changes field-less-window confidence merge; topology can stay identical | Implementation/merge comparison using compatible per-window observations and trusted revision-specific code | Aggregate confidence records cannot reconstruct missing window evidence; real-quality acceptance remains gated |

SHAs identify inspected inputs, not a permanent promise about branch contents. Refresh exact revisions and capabilities before generating evidence. PR 204 is stacked on the PR 203 branch; show any additional baseline drift rather than attributing all differences to the merge change.

At inspection, the common base is `b11555f4e6d2d2037711fb6ddda5dd01ce6dc952`. The direct PR 203-head → PR 204-head diff spans 45 files; the common-base → PR 204-head delta spans three. Compare each head to the common base for attribution, and mark later PR 203 changes as absent from PR 204's base. An adoption experiment against latest PR 203 requires a separately prepared candidate containing latest PR 203 plus the held CR6 delta; the raw older PR 204 head is not equivalent.

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

## Audit store compatibility

The audit viewer reads two shapes and never assumes the roadmap writers finished:

| Store shape | How it is recognized | What is verified | Honest limitation |
| --- | --- | --- | --- |
| Per-event chain (today) | `audit_log` without a unique `doc_id` index and no migration marker | Every row's v2 hash and the per-document `prev_hash` links, via `schemas.audit.verify_chain` | Many rows per document; no attempt sub-rows or addenda exist to verify |
| Unified row (foundation M2/M3) | `audit_meta` holds the migration marker, or `audit_log.doc_id` carries a unique index | Terminal row hash, its immediate predecessor link and `seq` continuity, each addendum hash + contiguous per-document `seq`/chain, and every attempt sub-row/reference exactly once through `audit.writer.attempt_hash` | Missing foundation writer package leaves attempt sub-rows `unverified`; the viewer never reimplements that hashing rule. This is a per-document scope, not a proof of the whole database chain |
| Migrated legacy section | `audit_log_legacy` present | The preserved per-document chains with `verify_chain` | Reported as a separate `legacy` section, never merged into the terminal row |
| No store / no schema / unreadable / no sync Postgres driver | File or table probe | Nothing | `unavailable` with a safe reason code; no path, URL or credential is returned |

Auxiliary table presence is not a shape signal: the foundation's `ensure_schema` creates `audit_attempts`/`audit_addenda`/`audit_meta` even while the migration is pending and `audit_log` still holds per-event rows. The migration marker (or a unique `doc_id` index) is authoritative; otherwise the store is read as an event chain.

All reads for one document run inside a single read-only snapshot (explicit SQLite `BEGIN`, Postgres `REPEATABLE READ`). A bounded read that is truncated is reported as `unverified`, never `verified`. Truncation never invents a failure: when the addenda section is cut short, an attempt whose parent lies in the unread part is unverified rather than an orphan, while a hash mismatch observed on a read row still fails. A marker that explicitly says `migrated: false` is ambiguous (the reader fails closed, it does not treat the store as unified). More than one terminal `audit_log` row for a document is `failed` with `duplicate-terminal-row`, because a marker-only store may lack the unique `doc_id` index; the detail read fetches two rows and the listing reports a counted `entries`, not an assumed `1`. The viewer does not create, migrate or initialize the storage schema. Startup migration is off on both SQLite and Postgres unless `MAILROOM_AUDIT_MIGRATE=1` is explicitly set. The deliberate `python -m storage.audit_migrate` CLI bypasses that knob on SQLite; Postgres always requires it. Opening the viewer requires neither migration nor this flag.

The document listing is bounded in *output* (`limit`/`offset`), not in scan work: it paginates a `GROUP BY doc_id` over the union of `audit_log` and `audit_log_legacy`, so a large store may scan/aggregate both tables per page.

**Verification scope.** This is deliberately narrower than the offline `audit.verify.verify_audit_db()` verifier, which walks the whole store. The HTTP viewer verifies one document: the row's hash, the link to its **immediate predecessor**, `seq` continuity, the addenda chain and every attempt sub-row/reference. It does not prove the rest of the global chain, so its verdict must not be described as a whole-database proof. No document-scoped verifier is imported or probed; the offline verifier remains the whole-store tool and is free to initialize/migrate explicitly.

**Integration needs:**

- `audit.writer.hash_entry(*, prev_hash, doc_id, entry_id, event, detail, matter_id, actor, timestamp)` / `attempt_hash(sub, *, matter_id=..., actor=...)` must remain importable without schema initialization, so a read-only consumer can recompute hashes exactly (no duplicated hashing rules). `hash_entry` takes keyword fields, not a row object.
- No read path may require `ensure_schema()`/migration; the viewer constructs its own read-only engine.

## Landing with the incoming PRs (historical inspection notes)

**Historical.** These notes describe the pre-merge inspection of PR 203 (`bee18cb0`) and PR 204 (`719f3816`). PR 203 landed on `main` at `1b9477c`; PR 204 remains held. Refresh heads before any new landing check; do not present `bee18cb0` as the current 203 revision.

1. Preserve the native feature branch. Prepare a disposable candidate from its exact commit and merge the trusted PR 203 head there; never switch a user's active checkout or run real-model collectors.
2. Retain both PRs' `CHANGELOG.md` Unreleased entries if they conflict, then regenerate `docs/changelog/` with `sync_gitbook_changelog.py`. Generated pages must never be hand-merged. Initial preflight found only these three changelog-related conflicts, with no implementation conflicts.
3. Install the candidate parent and nested tool into an isolated environment; verify subprocess imports originate in that candidate. Run nested tests, exporter/current-contract parity, routing/graph contracts, review-tail tests, mock experiment and generated-file checks. Confirm 14 nodes/48 edges and both human-applied report-tail connections on this PR 203 revision; baseline remains 14/46.
4. Prepare a second candidate by applying only PR 204's three-file merge-base delta (`b11555f4..719f3816`) onto the latest PR 203 candidate. Preserve later PR 203 fixes. Run CR6 regressions as well as the same native/contract checks; expect changed merge behavior with unchanged topology.
5. Record candidate commits/trees, merge-base, patch hash, dependency versions, actual commands/exits and failures in PR 208. Only call integration verified after these checks pass; a clean textual merge alone is insufficient. Repeat if any incoming head changes, and again after merge.

The native branch must remain usable on current main independently. This integration procedure does not approve PR 204's more permissive auto-archive behavior: its separate real held-out objective and risk guards remain required.

## Landing verification, 2026-11-09

Heads used to build the candidates: PR 208 `065a0c2`, PR 203 `ab090bd`, PR 204 `6b2d42a` (historical; PR 208 has since moved to tests/CI/docs-only commits, and a fresh review on head `d7e0b05` re-ran the full parent + nested suite and found the same PR 203 fixture result). Both candidates are disposable local merges, never pushed.

| Candidate | Built as | Result |
| --- | --- | --- |
| C1 | PR 208 + PR 203 (`git merge`). Conflicts only in `CHANGELOG.md` and the generated `docs/changelog/` pages: both Unreleased entries kept, pages regenerated | `sync_gitbook_changelog.py --check` ok (22 pages); `compileall` 0; `export_contract.py --check` current; nested + contract/routing/graph/review-tail/docs tests 267 passed, 1 failed; mock experiment gate exit 0; graph hash `sha256:036af6f5…cb8e` |
| C2 | C1 + the PR 204 delta (`git diff` from PR 203 head to PR 204 head, sha256 `73a6a06e0e960708…`, applied with `git apply --3way`, no conflicts) | same gates; with the CR6 suites added, 451 passed, 1 failed; mock experiment gate exit 0; graph hash **identical** to C1 (CR6 changes merge behaviour, not topology) |

The single failure in both is `test_cli.py::test_console_script_is_installed`. It comes from the verification environment, which put the tool on the path through a `.pth` file instead of `pip install -e`, so no console script exists. It is not a defect in the code.

`tests/fixtures/pr203-contract.json` is a verbatim copy of PR 203's contract: 14 nodes, 48 edges, START → {compile_report, extract, intake}, human_review → compile_report. `test_human_applied_start_edge_is_not_a_finding` pins it. After PR 203 merges, the committed contract this tool reads becomes that 48-edge contract, and `test_committed_contract_has_no_structural_findings` passed on it in C1.

Merge order: PR 203 → The-Mailroom PR 71 pin bump → PR 208 → PR 204 (retargeted to main, still held for its real `thresholds` objective). Repeat this check if any of these heads changes.
