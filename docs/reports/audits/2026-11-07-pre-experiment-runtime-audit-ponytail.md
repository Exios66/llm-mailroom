# pre-experiment runtime audit (ponytail)

- **Date**: 2026-11-07
- **Kind**: audits (Repository audit and synthesis reports)
- **Status**: findings as audited; remediation tracked below
- **Audited revision**: `19203ea` (the findings and line numbers below describe that commit)

## Remediation status (PR #202)

The findings below are kept as audited. PR #202 resolves them as follows:

| Issue | Status |
|---|---|
| #191, #192, #193, #194, #196, #197, #199, #201 | Fixed in PR #202, each with a regression test |
| #195 | Loader part fixed in PR #202: every parquet shard is loaded and a `content_sha256` mismatch is logged. The **yearless-date scorer fix is not done**: it belongs in `llm-dojo-scoring`, so yearless ground-truth dates still score against the run year until that lands and the pin moves |
| #198 | Superseded: main shipped v0.9.3 through the scripted release flow, and `release.py check` is OK. PR #202 no longer carries a release cut |
| #200 | Dead names removed and the inbox fallback goes through `list_inbox_files()` (`src/tests/test_lean_cleanup_200.py`). The `_merge_triage` / `_merge_sorter_reads` consolidation was **not done** |
| Sandbox (item 11) | Not part of this repo; tracked in `local-mailroom-sandbox` |

## Scope

The runtime-critical paths for the experiment week: the 13-node LangGraph
pipeline (`graph/`, `agents/`, `pipeline/`), the deterministic scoring stack
(`observability/`, `llm_dojo_scoring` wiring, `legalbench/` runners), the
experiment gates/runners (`run_experiment.py`, `run_pilot.py`,
`run_hf_pilot.py`), the API surfaces an operator touches (relations mode),
and the environment/pin chain this repo shares with `local-mailroom-sandbox`
(vendored copies + `llm-dojo-scoring` release pin).

Load assumption: one operator running mock + real (`qwen3.7-flash`,
`deepseek`, gateway tiers, free lane) experiment batches locally this week;
single-process watcher or pilot scripts — not multi-user production scale.

## Method

1. **Executed gates**: full pytest suite (2×), `run_experiment.py
   --dataset mailroom-fixtures --mock --baseline` (hermetic regression gate —
   PASSED, class_accuracy 1.0), `smoke_modal_tiers.py --check` (PASS),
   `run_hf_pilot.py --check` (pass but warn), `relations_mode status` (OK),
   `release.py check / status` (FAIL).
2. **Deep read**: two parallel coding-audit passes over
   `graph/build_graph.py`, `agents/*`, `pipeline/guards.py`, `pipeline/bins.py`,
   `observability/*` (field scoring, scores, hf corpus loader), `legalbench/`
   runners, `llm/client.py`, `llm/retry.py`, the three experiment runners.
3. **Verification discipline**: every finding was re-verified line-by-line
   against this checkout before an issue was opened; claims that failed
   reproduction were rejected and are recorded in the Findings as
   "checked-clean".

## Findings

All findings are tracked as GitHub issues #191–#201
(Exios66/llm-mailroom). Verified vs plausible is marked per finding.

### Must fix before real runs

1. **#192 (verified)** — Dojo scoring is never wired from taxonomy in any
   scoring entrypoint: `observability/scoring_wiring.py` is imported only
   inside `pipeline/relations.py:220` (guarded by `embeddings_enabled()`) and
   a test. `build_graph.py:3799,3816`, `run_pilot.py:546,556`,
   `run_hf_pilot.py:603,611`, `local_eval_packs.py:203` all score with dojo
   **package defaults**: heuristic field types replace the calibrated
   per-class maps, `type_bands` ignored, embedding rescue off. Label-typed
   ground truth (`claim_type`, `intent`, …) earns fuzzy partial credit. The
   experiment scores this week would not measure the calibration that the
   repo claims.
2. **#191 (verified)** — Grounded field scoring gets `doc_text=""`
   (`build_graph.py:3820` reads the pre-invoke snapshot; the real text lives
   on `result`), so the dojo verified-precision pass is silently skipped on
   every grounded trace, and trace scores diverge from report scores.

### Should fix (silent errors / data / reproducibility)

3. **#199 (verified)** — Intake sliding-window section offsets ignore the
   overlap prefix: every section from window 2 onward publishes wrong
   document-absolute offsets in the manifest (dormant consumer, bad data
   published).
4. **#193 (verified)** — `GET /api/relations/mode` runs `asyncio.run` inside
   the running event loop → `RuntimeError` → broad `except: pass` → the
   endpoint silently reports an empty ledger/edges. Confirmed by the
   never-awaited-coroutine warnings the test suite surfaced.
5. **#194 (verified mechanism)** — `move_to_failed` lacks the A-20
   collision protection `move_to_archive` has; two same-named failed
   documents destroy one silently.
6. **#196 (verified)** — six pipeline corpora float at the Hub tip
   (`revision: None`); `run_hf_pilot --check` warns and exits 0. A Hub
   publish mid-week silently changes ingested data.
7. **#195 (verified mechanics, dormant today)** — reproducibility pack:
   yearless date GT scores depend on the run year; `parquet_urls`
   last-shard-wins; `content_sha256` mismatches never block scoring.
8. **#201 (verified)** — `ensure_score_configs` caches a partial config set
   after a mid-loop Langfuse failure; later scores attach without
   `config_id` for the process lifetime.
9. **#198 (verified, environment)** — release chain FAIL: `pyproject 0.9.1`
   behind tagged `v0.9.3`; GitBook `docs/changelog/` stale. Heals the
   version/pin contract the sandbox vendor refresh depends on.
10. **#197 (verified) + comment** — legalbench accounting:
    `macro_f1 == 0.0` silently dropped from trace scores; stale
    `agent.usage()` double-counts on pre-call failures.
11. **Sandbox state (not an issue — a task)** — `local-mailroom-sandbox`
    sits on a 35-file uncommitted vendor refresh (dojo 0.28.x + llm-mailroom
    snapshot behind this checkout). The vendored copy must be committed and
    re-synced to pipeline main before sandbox runs are trusted.

### Nice to have

12. **#200 (verified)** — lean pack: zero-caller `move_to_classified` /
    `load_taxonomy` / `SUPPORTED_EXTENSIONS` (~60 lines), the near-duplicate
    `_merge_triage`/`_merge_sorter_reads` helpers, and the
    inbox-glob fallback that could ingest `.meta` sidecars.
13. Mock-gate honesty is good: the mock run's fixture parking in review is
    fail-closed by design (guardrail clamps, stage not gated) — no action.

### Checked clean (claims that did NOT reproduce)

- `metrics if "metrics" in dir() else None` guard (subagent claimed
  UnboundLocalError; direct repro on this venv shows unbound locals are NOT
  in `dir()` → guard fails soft as intended).
- `run_experiment --langfuse` error counting: the Langfuse SDK filters
  exceptions out of `item_results`, so `n_errors = len(items) -
  len(item_results)` is correct as written.
- `routing._transient_decision` off-by-one (subagent reworked → intent
  holds), `schemas._money` normalizer, review-park/legacy-ownership
  invariants, free-swarm retry/failover ladder, `llm/client.py` resolution —
  no defects found.
- Full pytest suite: **2767 passed / 37 skipped / 0 failed** (after
  re-priming the wheel-build cache; the single suite failure was the
  offline-build test needing the cache I cleared, not code).

## Recommendations (fix plan, ordered)

| Order | Issue | Fix | Test that fails before / passes after |
|---|---|---|---|
| 1 | #198 | `release.py status` → scripted `cut` heal for the version + `docs/changelog/` (not a hand-edit) | `release.py check` exits 0 |
| 2 | #192 | `import observability.scoring_wiring  # noqa: F401` at top of `build_graph.py`, `run_pilot.py`, `run_hf_pilot.py` | a build-graph scorere unit test asserting `claim_type` scores exact-match 0.0 on a wrong label |
| 3 | #191 | `doc_text=result.get("doc_text") or initial_state.get("doc_text")` | e2e test asserting `doc_verification.enabled` true on a grounded mock trace |
| 4 | #193 | `return await asyncio.to_thread(mode_status)` in the API handler (+ warn-only catches in `mode_status`) | existing `test_get_readout` without the RuntimeWarnings |
| 5 | #194 | mirror A-20 collision logic in `move_to_failed` | bins test: two same-named moves both survive |
| 6 | #199 | carry per-window prefix length into `_merge_sections` | intake test on a synthetic over-budget doc asserting offsets in-bounds & monotonic |
| 7 | #196 | pin reviewed shas in `hf_corpora.CORPORA`; make `--check` non-zero (or `--strict`) on unpinned | contract test |
| 8 | #201 | per-spec success caching; fetch failure bails | unit test with a mocked failing create |
| 9 | #197 | `.get(key, fallback)` chain; usage capture only on success row; comment tracks usage fix | `legalbench/langfuse_tracing` unit test for 0.0 macro_f1 |
| 10 | #195 | dojo date fix (yearless GT = month/day compare) + `setdefault` shard handling + integrity gate in `sync_dataset` | dojo field_scoring + loader tests |
| 11 | #200 | delete the three dead names; one merge helper; `list_inbox_files()` fallback | `pytest src/tests/test_lean_cleanup_200.py src/tests/test_api_upload.py src/tests/test_routing_hardening.py -q` |
| 12 | Sandbox | commit the in-flight vendor refresh; re-vendor `llm-mailroom @ main` post-#198; re-run sandbox gates | sandbox CI green on the committed branch |

After orders 1–8 land: re-run the two hermetic gates
(`run_experiment --mock --baseline`, `pytest`), then the real-run
pre-flight (`run_hf_pilot.py --check`, `smoke_modal_tiers.py --check`,
`relations_mode status`) before spending tokens.

## Not checked

- No real-LLM run was fired (`--real` paths worth ~$x of tokens were not
  spent from the audit; findings on real-mode behavior are code-verified).
- The standalone vendored Dojo scoring math beyond the audit's two scoring
  modules (Hungarian matching, ECE bins) — subagent deemed them clean.
- Sandbox runtime behavior (its checkout is untrusted until the vendor
  refresh is committed).
- `docs/wiki/`, deploy scripts, Gmail channel SMTP edge cases — out of the
  experiment-critical path.
