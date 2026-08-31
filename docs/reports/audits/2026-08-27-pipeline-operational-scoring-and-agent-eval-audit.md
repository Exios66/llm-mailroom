# Pipeline operational scoring and agent-eval audit

- **Date**: 2026-08-27
- **Kind**: audits (Repository audit and synthesis reports)
- **Status**: complete

## Scope

Operational / production finalization, managed-prompt coverage, agent
instruction suites, scoring completeness, and a methodology to evaluate
individual agents — plus live-manifest `insurance_claim` PDFs that complement
the local contrast pack. The approved plan file is not edited here.

## Method

- Code review of watcher finalization, archive missing-file paths, `/health`
  watcher lamp, prompt registry (`llm/prompts.py:prompt_templates`),
  `langchain_agents/skills/`, `observability/scores.py` + field scoring,
  and the new `observability/agent_eval.py` harness.
- Cross-check against Langfuse instrumentation and LangGraph persistence /
  HITL skills (native `interrupt()` remains a documented deferral).
- Ground-truth for insurance letters is the local pack in
  `observability/local_eval_packs.py` (approved / denied / partial Acme
  coverage determinations).

## Findings

1. **Archive missing-file paths could leave a claimed document unfinalized.**
   `archive_node` returning `stage=failed` without `_finalize_aborted` skipped
   the failed-bin move and catalog write. Patched to finalize.

2. **Stale `processing/` claims with a terminal manifest were requeued.**
   Crash recovery treated every orphan as "run again." A document that had
   already archived/failed/reviewed could bounce back into the inbox.
   `reconcile_stale_processing_file` now retires to `failed/` when a terminal
   manifest exists, else requeues. `recover_processing.py` uses the same rule.

3. **Watcher exceptions after `claim_file` stranded the document.**
   `run_pipeline` raising left the file in `processing/<worker_id>/`.
   `_finalize_claimed_on_error` now runs on that path.

4. **`GET /health` reported `ok` while the watcher lamp was `missing`/`stale`.**
   The lamp was already in `checks.watcher`; overall `status` did not degrade.
   It now reports `degraded`.

5. **`image_extractor` had no managed prompt.** It now registers
   `mailroom-image_extractor`, uses production doctrine, and keeps its own
   `taxonomy.yaml` agent block (`agent_name = "image_extractor"`).

6. **Instruction suites were incomplete.** Skill markdown now exists for the
   remaining specialists, reviewer, arbiter, boss, judge, reporter,
   pdf_transcriber, and image_extractor. `BaseAgent.system_prompt_with_skills()`
   appends them at call time (sorter / contracts specialist already had
   LangChain skill loading).

7. **Scoring gaps on grounded / in-pipeline judge paths.**
   - `stage_correct` was not emitted from `emit_pipeline_scores` when
     `expected_stage` was present.
   - In-pipeline `judge_verify` scores were not attached to the trace.
   - Field scoring had no three-way `deterministic_verdict` label; class
     mismatch did not force MISS.
   - `judge_band_high` was used in code but missing from taxonomy.
   - `conflict_threshold` was documented as a Boss routing knob; conflicts
     are field-value comparison in `_detect_conflict`.

8. **No per-agent eval methodology.** Live Langfuse evaluators target
   `pipeline-result` (two calls per document). There was no local way to
   score one agent against labeled cases. Added `run_agent_eval.py`.

9. **`insurance_claim` was a live class with no live-manifest PDFs.** Local
   packs covered approved / denied / partial, but `--mock` pilots never
   exercised the class end-to-end. Three synthetic letters now sit on the
   manifest (mock-only; `--real` refuses them via `is_real_sample`).

## Patches

| Area | Change |
|---|---|
| Production | `archive_node` finalize; watcher exception finalize; stale-claim reconcile; `/health` degrade |
| Prompts | `image_extractor` in `prompt_templates()` + doctrine (15 managed prompts) |
| Skills | Remaining `src/langchain_agents/skills/<agent>/` files; `system_prompt_with_skills()` |
| Scoring | `emit_in_pipeline_judge_scores`; `stage_correct`; `deterministic_verdict`; `judge_band_high`; conflict-threshold honesty |
| Agent eval | `observability/agent_eval.py` + `scripts/run_agent_eval.py` |
| Samples | `docs/examples/sources/insurance/claim_{approved,denied,partial}.txt` + manifest rows `insurance_01..03` (25 live samples; 10 synthetic mock-only) |

## Deferrals (intentional)

- Re-adding live per-agent Langfuse evaluators (cost: two pipeline judges
  already run per document).
- Braintrust `Eval()` loop (this repo's tracing is backend-agnostic;
  Braintrust is one of the `auto` backends, not the eval harness).
- Embedding `ops_monitor` in the API process.
- Multi-replica upload rate limit.
- LangGraph native `interrupt()` HITL (filesystem review bin remains the
  durability pattern; see 2026-08-12 LangGraph audit).
- Chunking long non-contract specialists.
- Wiring `extraction_category_presence` (no `presence_expectations` column
  in the manifest).

Insurance-claim PDFs were on the original deferral list and were **removed**
by a follow-up: they are now on the live manifest as synthetic mock-only
letters that complement the local packs, not as billed `--real` documents.

## Verification

```bash
PYTHONPATH=src pytest src/tests/test_pipeline_audit_eval.py \
  src/tests/test_prompts.py src/tests/test_samples_manifest.py \
  src/tests/test_real_sample_gate.py src/tests/test_langchain_capabilities.py \
  src/tests/test_kanban090_docclass_prompts.py src/tests/test_kanban085_fixture_expectations.py \
  src/tests/test_dataset_browser.py src/tests/test_api_upload.py \
  src/tests/test_watcher_reconcile.py -v
```
