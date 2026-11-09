# Pipeline hill-climb readiness: objective, calibration protocol and audit findings

- **Date**: 2026-11-08 (UTC)
- **Kind**: evaluations (offline audit and harness; no real model was run)
- **Status**: harness built and tested offline; thresholds NOT recalibrated yet — that
  needs next week's real sweep

## Why the current numbers cannot be tuned yet

1. **Experiments are oracle-assisted.** When `ground_truth` is in state, routing reads it
   (`src/graph/routing.py`: `class_misses_ground_truth` in `after_classify`,
   `after_retry_classify`, `after_review_classify`, `after_classification_arbiter`;
   `coverage_below_floor` in `after_extraction`). `run_pilot.run_sample` always passes it,
   so a wrong class can never auto-archive in an experiment, but can in production.
   Measured automation and accuracy do not transfer.
2. **Calibration data is overwritten.** The catalog keeps only each stage's last
   confidence; `review_classify_node` overwrites `classification_confidence`; retries
   write no audit entry.
3. **Self-reported confidence separates badly.** The decision-reviewer calibration
   (`docs/reports/evaluations/decision-reviewer/reviewer-calibration-luna-decisions.md`)
   found chat-model wrong answers at 0.86–0.99. The `confidence:` block in
   `src/config/taxonomy.yaml` has no calibration behind it.

## The hill-climb objective

Primary score, higher is better (`src/pipeline/policy_replay.py`, printed by
`src/scripts/hillclimb_eval.py` as `SCORE:`):

```
U = 1 − mean(cost per document) / C_h
cost = C_h · [human review or unobserved]  +  C_e · [auto-archived and wrong]
       + LLM USD / review_cost_usd
defaults: C_h = 1, C_e = 20, review_cost_usd = 4.00, field_floor = 0.80
```

`U = 0` is all-human with free LLM calls; `U = 1` is perfect free automation. An
auto-archive is correct only if the class matches gold **and** the archived extraction
attempt's Dojo field score is ≥ `field_floor`; an unscored auto-archive counts as wrong. The costs
imply Chow's rule: auto-accept only when P(correct) ≥ 1 − C_h/C_e = 0.95, which is the
calibrator's default target. **C_e and review_cost_usd are owner decisions** — change
them on the command line, not in code.

Guards (any failure → exit 2, the change is discarded):

| Guard | Rule |
|:--|:--|
| G1 | overall wrong-auto Clopper–Pearson 95% upper bound ≤ 0.02 |
| G2 | per-class wrong-auto rate ≤ 0.05 (classes with ≥ 30 auto-archives) |
| G3 | unobserved share ≤ 0.01 (replay fidelity) |
| G4 | mean LLM USD/doc ≤ `--cost-budget-usd` (off by default) |

Two counters in the `hillclimb_eval.py` report say how much of a score rests on weaker
evidence than a real observation:

- `schema evidence: N of M auto-archive(s) passed with schema_valid unknown`
  (`auto_archived_schema_unknown`). Live routing blocks only on an explicit
  `schema_valid is False` and replay mirrors that, so an attempt with no verdict passes. The
  sweep now records a bool whenever there is an extraction (a validator crash is recorded as
  `False` and listed in `diagnostics.schema_validation_errors`), so a non-zero count points at records from another producer
  (such as `export_review_labels.py` rows whose snapshot has no `extracted_data` payload, which
  leaves nothing to validate; a payload dict gets a bool, and a parked parse failure is `False`)
  or an older sweep, not at a sweep gap.
- `replay fidelity (next to G3): N route(s) reused an extraction ...`
  (`replayed_with_confidence_only_handoff_diff`). The sweep extracts once per candidate class;
  a collapsed candidate with another **subclass** demotes `observed.full_information` and its
  route is `unobserved` (G3), but one that differs **only in handoff confidence** (printed to
  2 decimals in the prompt) reuses the extraction and does not demote. That is an
  approximation, so the count shows how many routes lean on it.

Keep/discard: tune on `split=train`, score on `split=test`, and keep a change only when
the paired-bootstrap 95% interval of ΔU (`--baseline-policy`) has a positive lower bound
and all guards pass. Most gains come early; five flat iterations means the ceiling is architectural.

## Next week's protocol

1. **Collect** full-information records — every branch is run on every document
   (sorter attempt 1 and the retry, the reviewer, each extraction attempt, the judge),
   with no ground truth in the routing state and no input truncation:
   `PYTHONPATH=src .venv/bin/python src/scripts/run_calibration_sweep.py --hf-split train --per-class N --real --out train.jsonl`
   (refused unless `MAILROOM_ALLOW_REAL=1`; the owner runs it), then the same with
   `--hf-split test`.
2. **Calibrate** on train: `PYTHONPATH=src .venv/bin/python src/scripts/calibrate_thresholds.py --records train.jsonl
   --split train --out candidate.yaml`. It proposes per-class cutoffs only where the
   held-out Wilson bound clears the target and the sample suffices; other classes keep
   the taxonomy value and are marked `insufficient_evidence`.

   Extraction evidence (`judge_band_high`, `low`) pools **every** scored extraction attempt of
   the gold class, not only each document's final attempt. Routing archives any attempt whose
   confidence clears the cutoff, including attempt 1 before a retry would run, so scoring only
   the (usually better) final attempt would credit a confidently wrong attempt 1 to its retry
   and overstate accuracy at the cutoff. Counts in the table (`n`, `accept`, `err`) are
   therefore **attempts** (`unit` column), with the distinct documents behind them in `docs`.
   Attempts of one document are correlated, so the sample-size rule (`need`/`short`) counts
   distinct documents, and the held-out folds are grouped by document and judge a document by
   its worst accepted attempt. Sorter (`high`) evidence is one pair per document, unchanged.
3. **Score** on test: `PYTHONPATH=src .venv/bin/python src/scripts/hillclimb_eval.py --records test.jsonl --split test
   --policy candidate.yaml --baseline-policy src/config/taxonomy.yaml` (its `confidence:` block is
   the deployed policy; on later iterations pass the last accepted policy file instead).
   Leaving out `--baseline-policy` skips the ΔU comparison the keep rule needs.
4. **Iterate** on the policy file only, gating every iteration with
   `src/scripts/autoresearch_gate.py --objective thresholds` (core guards first, then the
   eval's guards and the ΔU interval; registry `experiments/autoresearch/objectives.yaml`,
   protocol `experiments/autoresearch/instructions.md`). Promote to `taxonomy.yaml` by a
   reviewed change.

Sample size: to show a wrong-accept rate ≤ 1% at 95% confidence a class needs 299
accepted documents with zero errors (473 with one, 628 with two; Dojo
`required_samples`); at 2% it needs 149 / 236 / 313. The corpus test split (323
documents) cannot prove per-class bounds for the small classes (merger_agreement has
about 17 test documents), so the protocol pools the overall bound (G1) and reports
per-class shortfalls instead of claiming them.

## Audit findings and status

Verified in code; "fixed" items have regression tests on this branch.

| Area | Finding | Status |
|:--|:--|:--|
| Routing | A failed extraction chunk was skipped silently and the merge took the MAX confidence | fixed: transient errors re-raise, failed chunks cap confidence below `low`, merge takes the MIN |
| Routing | Sorter window disagreement did not lower confidence | fixed: scaled by agreement share, capped below `low` when the winner lacks a majority |
| Routing | Conflict escalation ran before the schema/hollow/confidence gates, so the Boss could archive a bad extraction | fixed: checked after the gates; an unadjudicated conflict on the judge/arbiter tail parks |
| Routing | Conflict detection flagged "Delaware" vs "State of Delaware" and differently formatted dates | fixed: date and jurisdiction-aware comparison |
| Routing | Classify gets 1 retry although `retry_max: 2`; after the retry, low confidence skips the cheaper reviewer | open — a hill-climb lever, decide with data |
| Routing | Ground truth in routing state (above) | sweep keeps it out; live flag still open |
| Volume | Unbounded concurrency (a thread per inbox file) | fixed: capped worker pool (`watcher.max_concurrent_docs`, default 4) |
| Volume | Stale-claim reconcile can pull back a document that is still running | fixed for the watcher's own reconcile: claim stamps claim time and the reconcile skips files this process is running or has queued. The separate `recover_processing.py --apply` CLI cannot see another process's in-flight set and relies on the claim time vs `--stale-minutes` only; run it with the watcher stopped |
| Volume | Concurrent catalog and audit writes lost (`UNIQUE` races, `database is locked`) | fixed: upserts plus lock retry; 0 failed ops at 128 threads |
| Volume | Crash re-run creates duplicate rows; Docker stop grace below the drain timeout; outage parks everything to review; tracing outage slows each document ~20× | open |
| Review | Approving re-runs extraction instead of archiving what the person checked; corrections are not stored as labels | fixed: `approve_as_is` / `correct` with parked fields archive through the normal tail with no LLM call (a sorter-stage park has no fields, so `approve_as_is` confirms the class and re-extracts); `human_label` persisted; `export_review_labels.py` turns reviews into calibration records |
| Review | Most routes into review record no cause; queue is newest-first with no priority | fixed: cause coverage 85.1% → 100% on 13,692 reachable states; priority-then-age queue with SLA flags |
| Review | Reject/complete leak the interrupted checkpoint; concurrent resolves are unguarded | fixed: checkpoint discarded; per-document lock with 409; atomic manifest writes |

## Scale evaluator

`src/scripts/stress_test.py` is the scale hill-climb evaluator (mock LLM, integrity
gates, one `SCORE:` line). Same 8-document run (threads 1,4) before and after this
branch's fixes:

| Gate | Before | After |
|:--|--:|--:|
| lost_docs | 8 | 0 |
| inflight_hijacked | 3 | 0 |
| peak concurrent pipelines (cap 4) | 8 | 4 |
| db_failed_ops (32 + 128 threads) | 102 | 0 |
| duplicate_catalog_rows / duplicate_chains (crash recovery) | 4 / 4 | 4 / 4 (open) |

## Not verified

No real model was run: no real confidence distribution, cutoff or accuracy figure exists
yet. The synthetic generator (`src/pipeline/synthetic_records.py`) only validates the
harness. In mock mode the fixture rows carry no gold fields, so `extraction_score` is
empty and the objective counts those auto-archives as wrong by design.
