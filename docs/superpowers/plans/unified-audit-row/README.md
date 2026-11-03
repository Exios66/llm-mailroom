# Unified audit row: roadmap

<!-- roadmap:issue Exios66/llm-mailroom#150 -->

**Spec:** [`docs/superpowers/specs/2026-11-03-unified-audit-row-design.md`](../../specs/2026-11-03-unified-audit-row-design.md)
**Tracking issue:** [Exios66/llm-mailroom#150](https://github.com/Exios66/llm-mailroom/issues/150)

This folder is the single source of truth for progress. Each stage plan lists tasks as
`### Task <ID>: <name>` with `**Repo:**`, `**Spec:**` and `**Depends:**` lines and `- [ ]`
steps. `src/scripts/roadmap_status.py` reads them and does three things:

- regenerates the status table below (`--write`);
- fails (`--check`) when the table is stale, when a task cites a spec section that does not
  exist, when a numbered spec section has no task, when a task depends on an unknown task,
  or when a task is ticked done before a task it depends on;
- renders the body of #150 (`--issue-body`).

**This repository does not use GitHub Actions** (CI is CircleCI). `.github/workflows/roadmap-sync.yml`
is in the tree but never runs, and its red `roadmap` check on PRs is not a gate. So the
checks and the #150 sync are manual:

1. After ticking steps, run `python src/scripts/roadmap_status.py --write` and then
   `--check` (prints `roadmap: OK`), and commit the regenerated table with the ticks.
2. Update #150's body by hand: paste the output of `--issue-body` (keep the
   "Current state" bullets at its top current), using the GitHub tool or the web UI.

Progress is recorded by ticking steps in these files in the same PR that lands the work. Work in
another repository (dojo, sandbox, hub) is ticked here in a small follow-up PR that links
the merged PR in the step text (`- [x] **Step 3: ...** (Exios66/llm-dojo-scoring#120)`).

## Checkpointing (PRs)

- Each stage opens a **draft PR** in its repository as soon as its first task is
  committed, and pushes after every task. The PR body lists the stage's tasks as a
  checklist and is marked ready when the stage's last code task is green.
- After every task, the matching steps are ticked here (with the PR and commit) and
  pushed, so #150 shows the checkpoint. Before this folder is on `main`, ticks ride on
  PR #151; after that, they go in a small PR per checkpoint.
- Open checkpoint PRs: stage 2 → Exios66/llm-mailroom#160 (draft; M2 and M3 so far).
  Closed out: stage 0 → #151 (merged), stage 1 → Exios66/llm-dojo-scoring#116 and #118 (merged),
  M1 → #157 (merged), S0 → Exios66/local-mailroom-sandbox#240.

## Current state (handoff, 2026-11-03)

Read this first if you are picking the work up.

- **Merged to `main`:** the spec and plans (#151), dojo pin v0.27.1 `a82843e` (#157, M1),
  the release tool `src/scripts/release.py` (#158), CircleCI fixes (#159, #161).
- **Draft PR #160** (`claude/unified-audit-row-wip`, based on `main`): M2 (audit tables and
  migration, `ba73947`) and M3 (row and addendum writers, `3b6e5b9`). Steps are ticked on
  this branch; they reach `main` when #160 merges. Rulings for both are in the notes under
  each task in [stage-2-mailroom.md](stage-2-mailroom.md).
- **Open PR #162:** release cut v0.9.1 (`pyproject.toml` 0.8.1 → 0.9.1, after the existing
  `v0.9.0` tag). After merge the owner runs `release.py publish 0.9.1 --commit <sha> --apply --push`.
- **Next task: M4 (verification)** on the #160 branch, then M5 onward in order. Commit
  and push after every task, tick the steps here, and refresh #150.
- **Working notes:** the full mailroom suite takes about 5 minutes. On a cold uv cache,
  `test_wheel_runtime_catalog` fails until you run
  `uv build --no-sources --wheel --out-dir /tmp/uv-warm .` once. `ruff` is not a gate in
  this repo. `docs/changelog/` is generated: after a CHANGELOG merge conflict, run
  `python src/scripts/sync_gitbook_changelog.py`. Never hand-edit versions; use
  `src/scripts/release.py cut`.

## Stages

| Stage | Plan | Repo | Gate to start |
|---|---|---|---|
| 0 | (PR #113 in llm-dojo-scoring; tracker task R1 here) | llm-dojo-scoring, llm-mailroom | none |
| 1 | [stage-1-dojo.md](stage-1-dojo.md) | llm-dojo-scoring | v0.26.0 published |
| 2 | [stage-2-mailroom.md](stage-2-mailroom.md) | llm-mailroom | dojo v0.27.0 tag published |
| 3 | [stage-3-sandbox.md](stage-3-sandbox.md) | local-mailroom-sandbox | mailroom stage 2 merged to `main` |
| 4 | [stage-4-hub-and-follow-ups.md](stage-4-hub-and-follow-ups.md) | Digital-Mailroom, The-Mailroom, eval-environment, mailroom-issues | stage 3 merged |

The roadmap task R1 and Stage 0 live in [stage-0-roadmap.md](stage-0-roadmap.md).

## Status

<!-- roadmap:status:begin -->
**12/33 tasks done.**

| Stage | Task | Name | Repo | Steps | State |
|---|---|---|---|---|---|
| 0 | R0 | Merge PR #113 and publish v0.26.0 | llm-dojo-scoring | 3/3 | done |
| 0 | R1 | Roadmap tracker and #150 sync | llm-mailroom | 6/7 | in progress |
| 1 | D1 | Golden inputs and sandbox goldens | llm-dojo-scoring | 5/5 | done |
| 1 | D2 | `assess_extraction_payload` in the dojo | llm-dojo-scoring | 5/5 | done |
| 1 | D3 | Tier 0 common scorer and extraction scoping | llm-dojo-scoring | 5/5 | done |
| 1 | D4 | `DocScorer` protocol, registry and `generic` | llm-dojo-scoring | 5/5 | done |
| 1 | D5 | Type modules for contract and merger (benchmark headlines) | llm-dojo-scoring | 5/5 | done |
| 1 | D6 | Type modules for corporate record, correspondence and insurance claim | llm-dojo-scoring | 5/5 | done |
| 1 | D7 | `score_document` dispatcher and wider `score_archive_block` | llm-dojo-scoring | 5/5 | done |
| 1 | D8 | Release 0.27.0 | llm-dojo-scoring | 4/4 | done |
| 2 | M1 | Adopt dojo 0.27.0 | llm-mailroom | 4/4 | done |
| 2 | M2 | Audit tables and migration | llm-mailroom | 5/5 | done |
| 2 | M3 | Row and addendum writers | llm-mailroom | 4/5 | in progress |
| 2 | M4 | Verification | llm-mailroom | 0/5 | blocked |
| 2 | M5 | Trace collection in state | llm-mailroom | 0/5 | ready |
| 2 | M6 | Scoring node family | llm-mailroom | 0/5 | blocked |
| 2 | M7 | Row builder and terminal nodes | llm-mailroom | 0/5 | blocked |
| 2 | M8 | Human review paths and pre-row events | llm-mailroom | 0/5 | blocked |
| 2 | M9 | Trace sinks | llm-mailroom | 0/5 | blocked |
| 2 | M10 | Write-failure fallback and `audit-reconcile` | llm-mailroom | 0/5 | blocked |
| 2 | M11 | Readers and `audit_row_metrics` | llm-mailroom | 0/5 | blocked |
| 2 | M12 | Warehouse, `audit-report` and Langfuse scores | llm-mailroom | 0/5 | blocked |
| 2 | M14 | Rescore CLI | llm-mailroom | 0/5 | blocked |
| 2 | M15 | One scoring path (retire the legacy scorers) | llm-mailroom | 0/5 | blocked |
| 2 | M13 | Docs and branch gate | llm-mailroom | 0/3 | blocked |
| 3 | S0 | Capture sandbox output goldens | local-mailroom-sandbox | 3/3 | done |
| 3 | S1 | Vendor refresh and thin wrapper | local-mailroom-sandbox | 0/5 | blocked |
| 3 | S2 | Local audit rows and `audit-reconcile` in the sandbox CLI | local-mailroom-sandbox | 0/5 | blocked |
| 4 | H1 | Hub sync (mailroom-issues #240) | Digital-Mailroom | 0/3 | blocked |
| 4 | H2 | Warehouse readers (mailroom-issues #241) | Digital-Mailroom | 0/3 | blocked |
| 4 | F1 | The-Mailroom catch-up | The-Mailroom | 0/3 | blocked |
| 4 | F2 | eval-environment alignment | eval-environment | 0/2 | blocked |
| 4 | F3 | Issue close-out | mailroom-issues | 0/3 | blocked |
<!-- roadmap:status:end -->

## Cross-repo gap audit (2026-11-03)

After D6 the owner asked that the dojo, the mailroom pipeline and the sandbox all run on
the same specs. A read-only recon of all three found these gaps. G1–G11 are folded into tasks; G12 is
deferred and has no task in this roadmap.

| # | Gap | Folded into |
|---|---|---|
| G1 | The mailroom pins dojo 0.25.0 (`e35504db`), so the jump to 0.27.0 also brings 0.26.0's behaviour changes | M1 step 2 (diff and record moved scores) |
| G2 | The mailroom's vendored fallback snapshot is 0.25.0 | M1 step 2b |
| G3 | `extraction_gt.py` drops raw `cuad_clause_labels`, so the CUAD headline never fires in the mailroom | M6 files and `test_benchmark_rows_get_headline` |
| G4 | Three legacy scoring paths would disagree with the row | new task M15 |
| G5 | Scoring calls pass no `doc_subclass` | M6 `test_doc_subclass_from_final_state` |
| G6 | Two definitions of `schema_valid` (bool vs fraction) | M7 `audit-row-schema.md` |
| G7 | S1 said `merge_schema_adherence` and `classify_floor_mode` stay in the sandbox; the dojo already has them | S1 files |
| G8 | S1 left out files that must move in lockstep (`requirements/dojo-release.txt`, vendor READMEs, CHANGELOG) | S1 files and step 1 |
| G9 | The sandbox's vendored mailroom declares dojo 0.25.0 while the sandbox vendors 0.26.0 | S1 step 1 |
| G10 | Fallback labels must survive the wrapper | S1 step 2 (dojo tier 0 already keeps them; add goldens) |
| G11 | Public signatures must be confirmed before the cut | D8 step 1 (documented in D7) |
| G12 | Duplicated wilson/bootstrap helpers in the sandbox (low priority) | deferred; not in this roadmap |


## Adversarial review (2026-11-03)

The plan and the spec were reviewed for holes before execution. Each hole names the fix
and where it now lives. Fixes that changed the design were also written back into the spec
(spec revision 2), so the two stay aligned.

| # | Hole | Fix | Lives in |
|---|---|---|---|
| A1 | `score_archive_block` already takes `doc_text` and `expected_subclass` and already has a `method` key; the spec added a duplicate `scoring_method` and let MAUD/CUAD overwrite `overall_score`, changing an existing key's meaning (not additive). | `overall_score` keeps the field-map meaning; the MAUD/CUAD number goes to new `headline_score` / `headline_metric`; `method` keeps carrying `+maud` / `+cuad`; only `doc_subclass=` is new. `audit_row_metrics.overall_extraction_score` = `headline_score` if set, else `overall_score`. | spec §3, D6, M11 |
| A2 | Documents parked in the human tray have no row yet, so `record` / `requeue` decisions and early relation links had nowhere to go (addenda need a row). | Before the row exists these events are trace visits on the document (`human_review` decisions, `relations` visit) and land on the row; after it exists they are addenda. | spec §4.3, §5.4, M8 |
| A3 | Documents parked before the upgrade resume from an old LangGraph checkpoint with no `audit_trace` and old node names. | Row builder tolerates a missing or partial trace and sets `trace_incomplete: true`; test resumes a recorded pre-upgrade checkpoint. | spec §5.2, M5, M7 |
| A4 | Migration said "back up the DB file", which only exists on SQLite; `DATABASE_URL` can be Postgres, and several workers may start at once. | SQLite: file backup. Postgres: migration runs only with `MAILROOM_AUDIT_MIGRATE=1`, in one transaction, after the operator's `pg_dump`. Both run under the `audit_log:chain` lock and check `audit_meta` first, so a second worker is a no-op. | spec §6, M2 |
| A5 | No rollback: after migration, the previous mailroom release would write per-stage rows into a table with a unique `doc_id` and fail. | `audit_migrate.py --rollback` moves new tables to `*_v2_<timestamp>` and restores `audit_log` from `audit_log_legacy`; documented in the deploy notes. | spec §6, M2 |
| A6 | The pending spool (`audit_spool.db`) sits on ephemeral disk on Railway and is lost on redeploy. | `MAILROOM_AUDIT_SPOOL_PATH` (default next to the DB file); startup logs `audit_spool_ephemeral` when the path is not under `MAILROOM_DATA_DIR`; Phoenix (when enabled) is the durable copy. | spec §5.6, M10 |
| A7 | Golden parity needs recorded sandbox outputs, but tests are network-free and the dataset is remote. | D1 commits synthetic input rows covering every type, every subclass surface, MAUD/CUAD labels, no ground truth and prose payloads, and captures goldens with the current sandbox before any code moves. S0 captures sandbox output goldens before the vendor refresh. | D1, S0 |
| A8 | Ordering: goldens must be captured from the sandbox before the dojo code moves, or parity proves nothing. | D1 is a dependency of every other dojo task; S0 runs before S1. | D1, S0 |
| A9 | The dojo release fires `repository_dispatch` and `bump-dojo-scoring.yml` opens its own pin PR; hand-editing the pin would race it. | M1 adopts the workflow's PR rather than editing the pin. | M1 |
| A10 | Auto-sync of #150 only runs from `main`; these plan files are on a branch. | R1 lands the tracker and plans on `main` in a docs-only PR first. | R1 |
| A11 | `chain_length` and `entries` change meaning for existing `/audit/{doc_id}` readers (The-Mailroom, hub packages, eval-environment's `audit_ok`). | Additive keys only; each entry gains `kind`; consumers checked and listed in stage 4 (H1, F1, F2). | M11, stage 4 |
| A12 | The scoring block already has a `trace` key (trace knobs), easy to confuse with the row's `detail.trace`. | Names kept (renaming would break consumers); the row schema doc in M7 says which is which. | M7 |
| A13 | Digital-Mailroom, The-Mailroom, eval-environment and mailroom-issues are outside this session's GitHub scope. | Stage 4 tasks are written to run in sessions that have those repositories; they cannot be ticked from here. | stage 4 |
| A15 | Goldens captured from the sandbox at `main` would run its vendored dojo 0.25.0, so any 0.26.0 scoring change would show up as a false parity failure. | D1 captures with the dojo v0.26.0 checkout installed over the vendored copy and records the version it imported. | D1 |
| A16 | The dojo must not import mailroom, yet adherence parity needs mailroom's pydantic verdicts. | D1 records each case's `typed_valid`; D2's `GoldenSchemaLookup` stub replays it. | D1, D2 |
| A17 | On SQLite, `acquire_write_lock` does not cross processes (watcher and API), so two writers can read the same tail. | Unique index on `seq` as well as `doc_id`; the writer retries on a `seq` conflict after re-reading the tail. | M2, M3 |
| A18 | The spec names a rescore CLI (§5.3) and a rescore test (§8), but no task built it. | New task M14; M13 depends on it. | M14 |
| A19 | Spec §8's guard that `gt_metadata` and `corporate_record_*` stay unchanged (Mailroom-Corpus-EDA) had no task. | Added to D7's tests. | D7 |
| A20 | Updating a parked document's trace from the review API could bypass the reducer. | `graph.update_state(config, {"audit_trace": [visit]})`. | M8 |
| A21 | S0 depended on M13, which lets sandbox goldens be taken after other sandbox work has started; F2 depended on D8 but reads the new row. | S0 depends on D1 only; S1 depends on S0 and M13; F2 depends on M13. | S0, S1, F2 |
| A22 | Ticks for work done in other repositories could be forgotten, leaving #150 stale. | The last step of D8, M13, S2 and F3 is the tick PR; the PR check fails when the status table is stale. | D8, M13, S2, F3 |
| A14 | mailroom has no lint config, so "suite and lint pass" is undefined there. | Mailroom gate is the full `pytest` suite plus `python src/scripts/roadmap_status.py --check`; dojo and sandbox use their own ruff gates. | all stages |
