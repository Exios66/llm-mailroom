# Stage 3: sandbox vendor refresh and thin scoring wrapper

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The sandbox scores with the dojo's `score_document` and keeps every one of its own outputs unchanged. Its vendored graph writes local audit rows.

**Architecture:** First capture goldens of the sandbox's own outputs. Then refresh `vendor/` through `scripts/sync_vendor.py`, and turn `eval/scoring.py`, `eval/schema_adherence.py` and `eval/extraction_scope.py` into adapters over the dojo.

**Tech Stack:** Python 3.11, pytest, ruff. Work on branch `claude/dojo-0.27-wrapper` in `local-mailroom-sandbox`.

**Spec:** `docs/superpowers/specs/2026-11-03-unified-audit-row-design.md` §1, §3, §5.6, §8 (in llm-mailroom)

## Global Constraints

- Never hand-edit `vendor/**`, `requirements/*.txt`, `uv.lock` or `.env`. Refresh with `python scripts/sync_vendor.py` after editing `_VENDOR_PINS` in `src/mailroom_sandbox/cli.py`.
- Never launch paid or live work. Only `sandbox pilot --mock` is used.
- Coverage floor `fail_under = 77`; never lower it.
- The sandbox's outputs (`items.jsonl`, scores JSONL, `reports/experiment_log.jsonl`, serving JSON, Modal telemetry, cards, hub) must stay byte-identical on recorded inputs.
- Gate: `.venv/bin/python -m pytest -q -m "not local_llm"`, `ruff check src tests scripts deploy reports`, `python scripts/sync_requirements.py --check` and `python reports/dashboard/build_hub.py --check`.

## Review Focus

- A recorded run whose rows predate subclass fields → the wrapper gives the same payload as before.
- `sandbox pilot --mock` on a fresh checkout with no `data/runtime/` → the local audit DB is created, and the run does not fail.
- The vendored mailroom expects `MAILROOM_AUDIT_SPOOL_PATH` on Modal → it defaults under the Modal volume when mounted.
- The hub snapshot check after the vendor refresh → unchanged, or regenerated in the same commit.
- The `audit-reconcile` CLI run with no pending records → exit 0 with "nothing to reconcile".

---

### Task S0: Capture sandbox output goldens

**Repo:** local-mailroom-sandbox
**Spec:** §8
**Depends:** D1

This runs as early as D1 allows, before any sandbox change (review item A8).

- [ ] **Step 1: Write `tests/test_scoring_outputs_golden.py`.** It runs `score_extraction_row`, the scores-JSONL writer and the serving record on the dojo D1 inputs (copied to `tests/fixtures/scoring_golden/inputs.json`) and compares the results with `tests/fixtures/scoring_golden/outputs.json`.
- [ ] **Step 2: Generate `outputs.json`** with the current code (before the refresh), then run the test. Expected: PASS.
- [ ] **Step 3: Commit.**

### Task S1: Vendor refresh and thin wrapper

**Repo:** local-mailroom-sandbox
**Spec:** §3, §8
**Depends:** S0, M13

**Files:**
- Modify: `src/mailroom_sandbox/cli.py` (`_VENDOR_PINS` → dojo v0.27.0 and the mailroom stage 2 merge commit)
- Modify, in lockstep with the pins (gap G8): `requirements/dojo-release.txt` (checked by `tests/test_dojo_source.py`), `vendor/README.md`, the vendored `VENDOR.md` files and `CHANGELOG.md`. Regenerate the requirements with the repo's sync script; never hand-edit `requirements/*.txt`.
- Modify: `src/mailroom_sandbox/eval/scoring.py`. `score_extraction_row` calls `llm_dojo_scoring.score_document` with `schema_lookup=schemas.documents.get_extraction_schema`.
- Modify: `src/mailroom_sandbox/eval/schema_adherence.py` and `eval/extraction_scope.py`, which re-export the dojo names. The dojo already has `merge_schema_adherence` and `classify_floor_mode` too, so `schema_adherence.py` re-exports all of them (gap G7; the earlier note that they stay was stale). `aggregate_schema_adherence` delegates to `merge_schema_adherence`. `mock_llm.py` and the tests that import `schema_keys_for` and the scope helpers keep working through the re-exports.
- Test: `tests/test_scoring_outputs_golden.py`, `tests/test_scoring_ownership.py`, `tests/test_vendor_drift.py`

- [ ] **Step 1: Run `python scripts/sync_vendor.py`** after updating the pins, then the requirements sync check. Expected: `tests/test_vendor_drift.py` and `tests/test_dojo_source.py` pass. Add `test_vendored_mailroom_dojo_pin_matches` (gap G9): the dojo pin declared by the vendored llm-mailroom equals the sandbox's own dojo SHA. Today they differ (0.25.0 and 0.26.0).
- [ ] **Step 2: Replace the bodies with dojo calls.** Run the golden test. Expected: PASS. Every field score is identical; three outputs change on purpose (dojo 0.27.0 CHANGELOG): a `None` prediction is now a parse error, adherence reports keys outside the class schema, and correspondence/insurance rows gain topic/sentiment and claims-consistency extras. Re-record the affected S0 goldens in the same commit and list them in the PR. The wrapper keeps `doc_subclass` over `expected_subclass`; the dojo's tier 0 already keeps the `generic` / `suite-fallback-generic` labels (gap G10). Add goldens for a suite-lookup failure and a `suite.score` failure, so the labels are proved and not just assumed.
- [ ] **Step 3: Add `tests/test_decision_schema_contract.py`.** The `job/node_trace.py` decision keys must equal the mailroom fixture `src/tests/fixtures/audit/sandbox_decision_schema.json` in the vendored copy.
- [ ] **Step 4: Run the full gate.** Expected: PASS, with coverage at or above 77. Then, in llm-dojo-scoring, regenerate `tests/fixtures/consumer_surface.json` (`scripts/audit_consumer_surface.py --write`) so the scan picks up the sandbox's use of `score_document`.
- [ ] **Step 5: Commit.**

### Task S2: Local audit rows and `audit-reconcile` in the sandbox CLI

**Repo:** local-mailroom-sandbox
**Spec:** §5.6
**Depends:** S1

- [ ] **Step 1: Write the tests.**
  - `test_pilot_mock_writes_local_audit_rows`: one row per document in the sandbox DB, verified with the vendored `verify_audit_db`.
  - `test_reports_do_not_read_audit_rows`.
  - `test_audit_reconcile_cli_noop`.
  - `test_spool_defaults_to_modal_volume`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Add a `sandbox audit-reconcile` subcommand that calls the vendored `audit_reconcile.reconcile`. Spool path: when the Modal volume is mounted, use `<volume>/audit_spool.db`.
- [ ] **Step 4: Run the full gate.** Expected: PASS.
- [ ] **Step 5: Commit, open the PR (owner asks), and tick S0–S2** in llm-mailroom in a follow-up PR.
