# Confidence thresholds and review routing: calibration from existing runs

- **Date**: 2026-11-17 (UTC)
- **Kind**: evaluations (offline analysis of runs already made, plus mock checks; no model was called)
- **Status**: profile `calibrated_v1` is the default in `src/config/taxonomy.yaml`; the previous
  values are kept as `confidence_profiles.legacy_v0` for A/B (`MAILROOM_CONFIDENCE_PROFILE=legacy_v0`)
- **Follows**: [Pipeline hill-climb readiness](2026-11-08-pipeline-hillclimb-readiness.md), which
  said the `confidence:` block had no calibration behind it

## Summary

The thresholds were set by severity tier, not from data. The runs made since then show two
facts that decide everything else:

1. **Self-reported confidence is quantised.** The sorter answers 0.95 or 1.0 on 95% of
   documents; specialists answer 0.95 or 1.0 on nearly every extraction. A threshold between
   two of those values is a switch, not a dial, so each threshold either sends a whole class
   one way or does nothing.
2. **Within a class, the sorter's 0.95 is informative only for contracts.** Every other class
   is 100% correct at 0.95, so `high` above 0.95 only adds a retry, a reviewer call and, when
   the reviewer also says 0.95, an unneeded human review. Extraction confidence does not
   predict extraction quality in any class.

Changes (all in `confidence`, all per class):

| Class | Key | Old | New | Evidence |
|:---|:---|--:|--:|:---|
| insurance_claim | `high` | 0.98 | 0.95 | **solid**: 0 class errors in 1,100 predictions, 266 of them at 0.95 |
| corporate_record | `high` | 0.96 | 0.95 | **solid**: 0 class errors in 353 predictions, 208 at 0.95 |
| contract | `high` | 0.98 | 0.95 | **moderate**: see below; the one to watch |
| merger_agreement | `auto_archive` | (true) | false | **solid**: 0 correct archives in 155 merger document-runs |
| merger_agreement | `judge_band_high` | 0.97 | 0.90 (= `low`, empty band) | follows from `auto_archive: false` |
| merger_agreement | `arbiter_retry_max` / `judge_max_passes` | 2 / 3 | 1 / 2 | moderate; matches the sandbox production slice |

Everything else is unchanged, and the report says why below. Nothing was fitted on fewer than
about 100 documents per value, except where marked thin.

## 1. Inventory: every threshold, cap and review rule

Origin: **data** = fitted or checked against scored runs; **guess** = a default with no
calibration behind it; **rule** = deterministic, not a threshold.

| Node / stage | Setting (taxonomy key, code) | Value before | Origin | Decision here |
|:---|:---|:---|:---|:---|
| classify / retry_classify | `confidence.high`, `by_class.*.high` (`routing.after_classify`, `after_retry_classify`) | 0.97 global; contract, merger, insurance 0.98; corporate 0.96; correspondence 0.95 | guess (severity tiers) | refit (§2) |
| classify / retry_classify | `confidence.low`, `by_class.*.low` | 0.88; 0.90 / 0.90 / 0.90 / 0.86 / 0.85 | guess | kept: < 1% of sorter answers fall below 0.90 |
| classify | `retry_max` (also the extraction retry budget) | 2 | guess | kept (thin evidence that a retry never changes the class, §5) |
| review_classify (Lane A, LLM reviewer) | agree / override accepted at reviewer confidence ≥ class `high` (`build_graph.review_classify_node`, `after_review_classify`) | class `high` | guess | follows `high` |
| review_classify (decision reviewer) | `reviewer.default_mode`, `reviewer.by_class.*.cutoff` | `llm`, no cutoffs | data for the Decisions reviewer (sandbox `reports/reviewer-calibration/`) but not adopted | kept: the chat reviewer is the deploy baseline |
| classification_arbiter | LLM decision, no threshold | | rule | kept |
| extract | schema, hollow, lost-window and coverage gates (`after_extraction`) | | rule | kept |
| extract | `low` (retry / review below it) | as above | guess | kept: specialists rarely answer below 0.90 |
| judge_verify | `judge_band_high` (judge when `low ≤ conf < band`) | 0.95 global; 0.97 / 0.97 / 0.97 / 0.94 / 0.92 | guess | merger emptied; others kept (§3) |
| judge_verify | `judge_max_passes` | 3 | guess | merger 2 |
| judge_verify | `judge_rejudge_retry` (#241) | false | guess, waiting on #242 | kept |
| arbiter | `arbiter_retry_max` | 2 | guess | merger 1 |
| compile_report | `keep_best_attempt` (#254) | false | guess, waiting on #242 | kept |
| boss_escalation | matter conflict (deterministic, `_detect_conflict`); `conflict_threshold` 0.3 is unused | | rule | kept |
| compile_report → catalog_write | report must assemble (`after_report`) | | rule | **new**: per-class `auto_archive` |
| decision points | `decision_points` | none configured | | kept |
| human_review tray | `review_queue.cause_weights`, `class_tier_weight`, `tier_cutoffs`, `sla_hours` | | guess (no reviewer outcome data yet) | kept; new cause weighted 1.0 |
| field scoring | `field_scoring.ambiguous_band`, `type_bands` | | data (`scripts/calibrate_field_scoring.py`, #4) | kept |
| relations | `similarity_threshold` 0.62, `keyword_jaccard_threshold` 0.25 | | guess | out of scope |
| ops monitor | `review_backlog.threshold` 20 and the other pager rules | | guess | out of scope |
| sandbox quality bar | `config/production/bars.yaml` 0.90 (local-mailroom-sandbox) | | owner decision | not a pipeline threshold; used to score |

The sandbox's production slice (`config/runs/production/slice-0100.yaml`) already overrode
contract and corporate `high` to 0.95 and the merger budgets to 1 / 2, so the studies below ran
on those values while this repository's defaults stayed at the old ones.

## 2. Classification: the sorter's `high`

**Data:** `data/handoff/sorter-qwen3.7-flash/{train,test}.targets.jsonl` in local-mailroom-sandbox:
3,297 Qwen 3.7 Flash sorter answers with corrected targets (labels, not sorter output). Reliability
by stated confidence:

| Sorter confidence | n | Class accuracy |
|--:|--:|--:|
| 0.10 / 0.50 | 6 | 0% (all `unknown`) |
| 0.85 / 0.92 | 12 | 100% |
| 0.95 | 1,962 | 93.4% |
| 0.96 / 0.98 | 144 | 100% |
| 1.00 | 1,173 | 100% |

Pooled, 0.95 looks weak. Split by the class the sorter predicted, which is what routing sees:

| Predicted class | n | at 0.95 | wrong at 0.95 | wrong at ≥ 0.98 |
|:---|--:|--:|--:|--:|
| contract | 696 | 545 | 100 (18%) | 0 |
| correspondence | 962 | 906 | 1 | 0 |
| insurance_claim | 1,100 | 266 | 0 | 0 |
| corporate_record | 353 | 208 | 0 | 0 |
| merger_agreement | 152 | 9 (+2 at 0.96) | 0 | 0 |
| unknown | 34 | | all wrong (routed to a person already) | |

So the 0.95 errors are one pattern: indentures and rights instruments called contracts (89 of
the 100; the rest are misfiled emails and articles). The sorter's `high` above 0.95 buys nothing for insurance and
corporate records, and costs a retry, a reviewer call and sometimes a person.

**What the band costs, end to end.** Lane A was simulated on the 323-document held-out test set by
joining the sorter answers with the Qwen chat reviewer's answers on the same documents
(`reports/reviewer-calibration/reviewer-validation-qwen-flash-test.items.jsonl`), through the
same agree/override-at-`high` rule as `review_classify_node` (a retry is assumed to return the
same answer, which the quantisation makes likely):

| Profile (old sorter prompt) | Auto, correct | Auto, wrong class | Person, needed | Person, not needed |
|:---|--:|--:|--:|--:|
| legacy_v0 | 280 | 3 | 8 | 32 |
| insurance + corporate at 0.95 | 294 | 3 | 8 | 18 |
| calibrated_v1 (contract at 0.95 too) | 310 | 9 | 4 | 0 |

**Contract is the judgement call.** On the old sorter prompt, contract at 0.95 lets 6 more
indentures through as contracts (3 → 9 wrong of 323). The current sorter prompt
(`sorter_condensed_v5`, the vendored default) fixes that pattern: on the 275-document prime sample
it fixed all 87 corporate→contract errors and broke none
(`reports/sorter-handoff/sorter-condensed-v1-qwen-train-prime-sample.compare.json`; v5 adds one
corporate rule). The studies that ran on condensed v5 with contract at 0.95 (autoresearch v1 and
v2, 100 documents, each run twice, and the ladder's E3 hold-out, 45 documents) archived no document
under a wrong class. Against that: the old threshold sent about a third of correct contracts to a
person (18 of 59 in the simulation), with the reviewer's own 0.95 as the only reason. So 0.95 is
adopted, rated moderate, and the next real run must report the contract wrong-class rate. If an
indenture slips through, put contract back to 0.98 (or use the legacy profile) until the sorter or
a Decisions reviewer handles it.

**Merger `high` stays 0.98** (thin: 11 predictions below 0.98, none wrong). It matters little now:
every merger reaches a person anyway (§3).

**Reviewer acceptance follows `high`.** The Qwen chat reviewer is 100% right at 0.95 for insurance
(11), corporate (8), correspondence (60) and merger (2), but 90% for contract (20) and 93% at 1.0
(43); its contract confidence does not separate either. It runs on a contract only when the sorter
is below 0.95 (12 of 696), so the exposure is small.

## 3. Extraction: judge band, archive and budgets

**Data:** local-mailroom-sandbox `reports/autoresearch/mailroom-v{1,2}/scores/*.json` (25 and 75
documents, Hub > interim > near_gold truth, each baseline run twice), the v1 per-node failure
analysis (a project note, not in a repository), and the 2026-11-15 audits in local-mailroom-sandbox
(`contract-recall-routing-ladder.md` and `pipeline-qwen-flash-25-failure-modes.md`, both under its
audits folder).

Outcomes of the four baseline runs (v1 ×2, v2 ×2; 40 document-runs per class; C correct archive,
W wrong archive, R review, U archive with no scorable gold):

| Class | judged: C / R / W / U | not judged: C / R / W / U |
|:---|:---|:---|
| contract | 3 / 21 / 15 / 0 | 0 / 0 / 1 / 0 |
| corporate_record | 2 / 1 / 0 / 0 | 9 / 0 / 3 / 25 |
| correspondence | 0 / 1 / 3 / 3 | 0 / 0 / 7 / 26 |
| insurance_claim | 1 / 3 / 1 / 0 | 23 / 0 / 12 / 0 |
| merger_agreement | 0 / 3 / 14 / 0 | 0 / 1 / 22 / 0 |

**Extraction confidence does not predict quality.** In v1, archived mergers that reported 1.0 had
mean quality 0.15 (n = 8), those at 0.95 had 0.21 (n = 3). A contract at 0.98 scored 0.69 in all
four runs. Every corporate record reports ≥ 0.95 whatever it scores (0.16 at 1.0 in r2). The judge
band therefore picks documents by an uninformative number.

**The judge does not separate good from bad either** (#242): a final `complete` verdict was
followed by a wrong archive 6 of 7 times in v1 (contract, merger, insurance); it passed 3 of 46
contract calls in E1; on 18 corporate records (E2) it marked both records below 0.5 `complete`
and sent 6 others to review, 5 of which scored 1.00. Judging every corporate record cost 2× the
tokens and changed no score. So the bands are **kept** for contract, insurance, corporate and
correspondence: moving them cannot be justified until the judge is calibrated, and widening them
was measured not to help.

**Mergers always go to a person.** No merger extraction met the 0.90 bar in any run: 0 correct archives in
155 merger document-runs across every v1/v2 round (46 wrong archives, 103 reviews, 6 crashes), MAUD
accuracy about 0.10 to 0.15 (E3: 0.106 at about 410k tokens per document). An archive of a merger is therefore always a wrong archive (-1),
a review costs -0.25. All 36 merger archives in the baseline table above were wrong, 22 of them never judged. With
`auto_archive: false` a merger is extracted once, its report compiled, and the draft parked for a
person with the cause `class_requires_review`. Its judge band is emptied (band high = `low`), so
the judge/arbiter loop that cost most of a run's tokens without changing the outcome no longer
runs on confident extractions. Expected effect on the v2 draw: 14 wrong archives become reviews,
about +0.14 mean utility (the study's noise floor is 0.084). Mergers are the lowest-priority class,
so a person handling them is the intended deploy behaviour until merger extraction improves.

**Budgets.** Merger `arbiter_retry_max` 1 and `judge_max_passes` 2 follow the 25-document audit (1 of
17 re-judged retries improved a verdict) and match the production slice. Thin, and moot while
mergers park.

## 4. Mock check: does each node route as designed, and does review fire only when it should?

Run on this branch, both profiles, no model calls (`--mock` answers each sample's gold class at
the sample's confidence, so these checks prove wiring, not quality):

| Check | calibrated_v1 | legacy_v0 |
|:---|:---|:---|
| `run_pilot.py --mock --dataset hf --per-class 2` (10 Hub documents) | 8 archived, 2 review (both mergers, cause `class_requires_review`), class accuracy 1.0 | 10 archived, 0 review |
| `run_experiment.py --dataset mailroom-fixtures --mock --baseline …` | pass, class accuracy 1.0 | pass, class accuracy 1.0 |
| `src/tests/test_confidence_calibration.py` | 46 tests: profile selection, every changed value, legacy values verbatim, the sorter band per class, reviewer acceptance at 0.95, the merger judge band, the archive park, the cause, and a parked merger that a person approves archiving through `resume_from_review` (no loop) | covered by the same tests |

Human-review interaction: a parked merger archives once a person applies or corrects its fields
(`human_applied`). A plain class approval (`approved`) re-extracts, which is new model output, so it
parks again for the person to approve the fields; it never loops without a person acting.

## 5. Not verified, and what to measure next

- **No real-model run.** OpenRouter credits are exhausted, so every number is from runs made before
  today. The sorter and reviewer data used the older sorter prompt; the studies used condensed v5.
- **New prompts move confidence.** The deploy_v1 / prompt-alignment prompts may shift what the
  specialists and the sorter report. Refit after they land: re-run the sorter handoff export
  (`scripts/export_sorter_handoff.py` in the sandbox) and the routing audit
  (`sandbox run routing --run-id <id>`), then edit the `confidence` block. The legacy profile stays
  for the A/B arm.
- **Thin or untested:** merger `high`; `retry_max` for classification (the 25-document audit saw no
  retry change a class, but that is one run); `judge_rejudge_retry` and `keep_best_attempt` (waiting
  on judge calibration, #242); review-queue weights (no reviewer outcome data yet).
- **First real run must report:** contract wrong-class archives (the moderate change), review rate
  per class against legacy_v0 on the same draw, and merger tokens per document.
