# Stage 0: release v0.26.0 and the roadmap tracker

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship dojo v0.26.0 and land the tracker that keeps this roadmap and #150 current.

**Architecture:** The tracker is a stdlib-only script that parses the stage plans and the spec headings. A GitHub workflow runs it as a PR check and as the #150 updater.

**Tech Stack:** Python 3.11 stdlib, GitHub Actions, `gh` CLI with `GITHUB_TOKEN`.

**Spec:** `docs/superpowers/specs/2026-11-03-unified-audit-row-design.md` §9

## Global Constraints

- Dojo: never hand-edit versions; use `python scripts/release.py cut X.Y.Z` (dry-run unless `--apply`); never force-push, delete tags, or amend release commits; `uv.lock` is generated (`uv lock`).
- The tracker has no third-party dependencies and makes no network calls; only the workflow talks to GitHub.

## Review Focus

- A plan file with a malformed task heading (no ID) → `--check` fails naming the file and line, rather than silently dropping the task.
- A step ticked `[x]` in a task whose dependency is not done → `--check` fails.
- The issue body exceeds GitHub's 65,536-character limit → the body is cut at the stage table and links to the plans.
- The workflow runs on a fork PR (no write token) → the check runs, and the issue update is skipped.
- The spec gains a new numbered section → `--check` fails until a task cites it.

---

### Task R0: Merge PR #113 and publish v0.26.0

**Repo:** llm-dojo-scoring
**Spec:** §9
**Depends:** none

- [ ] **Step 1: Merge PR #113** (owner action) after CI is green.
- [ ] **Step 2: Move the `[Unreleased]` entries into `[0.26.0]`** in `CHANGELOG.md` on `main` with today's date, then `python scripts/release.py check`. Expected: `OK`.
- [ ] **Step 3: Publish** with `python scripts/release.py publish 0.26.0` from the merge commit (dry run, then `--apply`). The commit `2fea5ea` is refused because its uv.lock is stale, which is expected.

### Task R1: Roadmap tracker and #150 sync

**Repo:** llm-mailroom
**Spec:** §9
**Depends:** none

**Files:**
- Create: `src/scripts/roadmap_status.py`
- Create: `src/tests/test_roadmap_status.py`
- Create: `.github/workflows/roadmap-sync.yml`
- Modify: `docs/superpowers/plans/unified-audit-row/README.md` (status block)

**Interfaces:**
- Produces: `parse_plans(plan_dir: Path) -> list[Task]`, `spec_sections(spec: Path) -> set[str]`, `check(plan_dir, spec) -> list[str]` (errors), `render_status(tasks) -> str`, `render_issue_body(tasks, readme) -> str`. CLI: `--write`, `--check`, `--issue-body`.

- [x] **Step 1: Write failing tests**: parsing a task with ticked and unticked steps; a missing spec section; an uncited spec section; an unknown dependency; a task ticked done before its dependency; a stale status block; the issue body staying under 65,536 characters.
- [x] **Step 2: Run** `pytest src/tests/test_roadmap_status.py -q`. Expected: FAIL (module missing).
- [x] **Step 3: Implement `src/scripts/roadmap_status.py`.** A section ID is `§N` or `§N.M` taken from the spec's `## N.` / `### N.M` headings. `§1` and `§2` are exempt from the coverage check (goal and staleness). A task is done when every step is ticked.
- [x] **Step 4: Run** the tests. Expected: PASS. Then `python src/scripts/roadmap_status.py --write && python src/scripts/roadmap_status.py --check`. Expected: exit 0.
- [x] **Step 5: Add `.github/workflows/roadmap-sync.yml`.** On `pull_request` with paths `docs/superpowers/**` or `src/scripts/roadmap_status.py`, it runs `--check`. On `push` to `main` with the same paths, it runs `--check` and then `gh issue edit 150 --body-file <(python src/scripts/roadmap_status.py --issue-body)`. Permissions: `contents: read`, `issues: write`.
- [x] **Step 6: Commit** on `claude/unified-audit-row-spec`.
- [ ] **Step 7: Land on `main`** in a docs-and-tooling PR (owner merges), so the push trigger starts updating #150 (review item A10).
