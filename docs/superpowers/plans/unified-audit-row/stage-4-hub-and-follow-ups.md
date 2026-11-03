# Stage 4: hub sync, warehouse readers and follow-ups

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the hub and the outer consumers in line with the row contract, and close out the issues.

**Architecture:** The hub imports packages; it is never the source. The-Mailroom and eval-environment read the published `/audit` contract. Run these tasks from sessions that have those repositories (review item A13).

**Tech Stack:** per repository.

**Spec:** `docs/superpowers/specs/2026-11-03-unified-audit-row-design.md` §1, §2, §7, §9 (in llm-mailroom)

## Global Constraints

- Hub: `packages/*` are integration copies. Refresh with `sync_packages.py` and never edit them by hand. Never export hub edits back into the standalone repos.
- Consumers adapt to the contract; the contract does not bend to an outdated consumer.

## Review Focus

- A hub reader that counts `chain_length` as stages → counts by `kind` after the change.
- Warehouse Parquet from before the change, read beside the new files → readers handle missing `audit_rows_*` files.
- The-Mailroom showing a document with only legacy rows → renders the legacy section.
- eval-environment's `audit_ok` on a sandbox run that has no row because the write is pending → reports `pending`, not failure.
- Issue comments posted while a stage is still open → each comment says which stage closes the item.

---

### Task H1: Hub sync (mailroom-issues #240)

**Repo:** Digital-Mailroom
**Spec:** §9
**Depends:** S2

- [ ] **Step 1: Run `sync_packages.py`** for llm-mailroom, the dojo (jumping from 0.24.2 to 0.27.0) and the sandbox, then `taxonomy_parity.py`. Expected: both pass.
- [ ] **Step 2: Find hub readers of `/audit`** (`grep -rn "chain_length\|/audit/" packages apps`) and move them to `kind`-aware reading, with tests.
- [ ] **Step 3: Run the hub suite.** Expected: PASS. Commit and open the PR (owner asks).

### Task H2: Warehouse readers (mailroom-issues #241)

**Repo:** Digital-Mailroom
**Spec:** §7
**Depends:** H1

- [ ] **Step 1: Write tests that read** `audit_rows_*`, `audit_attempts_*`, `audit_addenda_*` and `audit_legacy.parquet` through the warehouse manifest, with the old-file case (Review Focus).
- [ ] **Step 2: Implement the readers** and the per-class report view over `audit_row_metrics` columns.
- [ ] **Step 3: Run the hub suite.** Expected: PASS. Commit and open the PR (owner asks).

### Task F1: The-Mailroom catch-up

**Repo:** The-Mailroom
**Spec:** §7, §9
**Depends:** M13

- [ ] **Step 1: Fix `AuditChain.tsx`**, which reads `action`/`hash`/`id` while the API returns `event`/`entry_hash`/`entry_id`. Nest sub-rows and addenda by `kind`/`parent_id`, and show the legacy section.
- [ ] **Step 2: Add stations** for `score_common`, the `score_<type>` nodes and `audit_failed` in `pipeline_schema.SPAN_STAGE_MAP`.
- [ ] **Step 3: Run its tests and build.** Commit and open the PR (owner asks).

### Task F2: eval-environment alignment

**Repo:** eval-environment
**Spec:** §1, §9
**Depends:** M13

- [ ] **Step 1: Make the archivist task's `audit_ok` read the row's `event`** and accept `pending` (Review Focus).
- [ ] **Step 2: Optional: replace its `extraction_scope.py` copy** with `llm_dojo_scoring.scoring.extraction_scope`. Run its suite. Commit.

### Task F3: Issue close-out

**Repo:** mailroom-issues
**Spec:** §2
**Depends:** H2, F1, F2

- [ ] **Step 1: Post the staleness notes** (spec §2) on #235–#239, each naming the stage that closes it. Needs access to LLM-Mailroom-Services/mailroom-issues.
- [ ] **Step 2: Close #236–#239 when M13 is merged** and #235 when M12 is merged, and #240 when H1 is merged and #241 when H2 is merged. Use `state_reason: completed` and link the PRs.
- [ ] **Step 3: Tick H1–F3** in llm-mailroom's stage 4 plan in a follow-up PR that links each merged PR.
