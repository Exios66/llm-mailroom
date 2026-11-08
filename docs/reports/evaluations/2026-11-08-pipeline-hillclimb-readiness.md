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
auto-archive is correct only if the class matches gold **and** the final extraction's
Dojo field score is ≥ `field_floor`; an unscored auto-archive counts as wrong. The costs
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

Keep/discard: tune on `split=train`, score on `split=test`, and keep a change only when
the paired-bootstrap 95% interval of ΔU (`--baseline-policy`) excludes 0 and all guards
pass. Most gains come early; five flat iterations means the ceiling is architectural.

## Next week's protocol

1. **Collect** full-information records — every branch is run on every document
   (sorter attempt 1 and the retry, the reviewer, each extraction attempt, the judge),
   with no ground truth in the routing state and no input truncation:
   `src/scripts/run_calibration_sweep.py --hf-split train --per-class N --real --out train.jsonl`
   (refused unless `MAILROOM_ALLOW_REAL=1`; the owner runs it), then the same with
   `--hf-split test`.
2. **Calibrate** on train: `src/scripts/calibrate_thresholds.py --records train.jsonl
   --split train --out candidate.yaml`. It proposes per-class cutoffs only where the
   held-out Wilson bound clears the target and the sample suffices; other classes keep
   the taxonomy value and are marked `insufficient_evidence`.
3. **Score** on test: `src/scripts/hillclimb_eval.py --records test.jsonl --split test
   --policy candidate.yaml --baseline-policy <current>`.
4. **Iterate** on the policy file only; promote to `taxonomy.yaml` by a reviewed change.

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
| Volume | Unbounded concurrency (a thread per inbox file) | in progress |
| Volume | Stale-claim reconcile can pull back a document that is still running | in progress |
| Volume | Concurrent catalog writes lost (`UNIQUE` races, `database is locked`) | in progress |
| Volume | Crash re-run creates duplicate rows; Docker stop grace below the drain timeout; outage parks everything to review; tracing outage slows each document ~20× | open |
| Review | Approving re-runs extraction instead of archiving what the person checked; corrections are not stored as labels | in progress |
| Review | Most routes into review record no cause; queue is newest-first with no priority | in progress |
| Review | Reject/complete leak the interrupted checkpoint; concurrent resolves are unguarded | in progress |

## Not verified

No real model was run: no real confidence distribution, cutoff or accuracy figure exists
yet. The synthetic generator (`src/pipeline/synthetic_records.py`) only validates the
harness. In mock mode the fixture rows carry no gold fields, so `extraction_score` is
empty and the objective counts those auto-archives as wrong by design.
