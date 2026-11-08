# Autoresearch Instructions — Round 1: field_scoring bands (llm-mailroom)

## Goal
Raise the share of extracted fields the deterministic scorer can settle **without calling the LLM judge**, while not
letting it settle more of them wrongly. One file, one metric, one loop: edit, run the eval, keep or revert, repeat.

## System context
`llm_dojo_scoring` (frozen upstream package) scores each extracted field against ground truth (0..1). The
`field_scoring:` block of `src/config/taxonomy.yaml` sets **bands** per field type: a score inside the band is
"ambiguous" and escalates to the LLM judge (one paid call); below the band is auto-rejected, above it auto-accepted.
Narrower bands save judge calls but risk wrong auto-decisions. The eval wires the taxonomy into the Dojo scorer the same
way the pipeline does (`configure_from_taxonomy`) and replays labeled (prediction, ground-truth) pairs built from real
corpus labels (Mailroom-Corpus-EDA v9.3): ~240 docs, ~8k pairs per perturbation family, ~2.5 s per run, no network, no LLM.

## What you CAN modify
Only these keys inside `field_scoring:` of `src/config/taxonomy.yaml`:
1. `type_bands` — `date`/`id` are `never` (exact-after-normalize; do not change); tune `money`, `free_text`, `name`,
   `entity_list`. Current: money [0.675,0.938], free_text [0.6,0.95], name [0.5,1.0], entity_list [0.5,1.0].
2. `ambiguous_band` (global fallback, [0.5,0.85]).
3. `bipartite_match_threshold` (0.6), `embedding_rescue_below` (0.7), `embedding_enabled`.
4. `factuality_verification.token_coverage` — NOT exercised by this eval; leave it alone.
Keep every YAML comment that explains a value; update the comment if you change the value.

## What you CANNOT modify
- `experiments/autoresearch/field_scoring/eval.py`, `build_cache.py`, `baseline.json`, `.autoresearch_cache/` — frozen.
- Anything else in `taxonomy.yaml` (confidence thresholds, agents, bins, cost models…), and any Python file.
- `vendor/llm-dojo-scoring/` and the Dojo pin: scoring bugs are fixed upstream in `Exios66/llm-dojo-scoring`.
- `containment_fields`, `partial_gt_fields` (definitions of what counts as correct — changing them games the metric).
- No `--real` runs, no network/LLM calls, no new dependencies, no linter.

## Eval
```bash
cd /home/user/llm-mailroom && PYTHONPATH=src .venv/bin/python experiments/autoresearch/field_scoring/eval.py
```
Prints a per-type table, then `GUARD_PASS: True|False` and `SCORE: x.xxxx`.
- **SCORE** = macro-average over field types of P(auto-decided **and** right) on the *dev* family. Higher is better.
- **Guard** (heldout family is a different perturbation generator; it never feeds SCORE): the wrong-auto-decision rate
  may not exceed `baseline.json` + 0.005 for any type, or + 0.003 overall, on dev **or** heldout. A change that raises
  SCORE but prints `GUARD_VIOLATION` is `guard_fail` → revert.
- **Baseline: SCORE 0.8362** (3 identical runs; the eval is deterministic, variance 0). **Min-delta: 0.002.**

## Known limits — read before celebrating a gain
- Labels are correct/incorrect *by construction* from perturbations, not from human review. Beating the dev family only
  proves something if the heldout guard holds; treat any gain as a hypothesis for human review, not a verdict.
- `name` auto-wrong is ~24% and does **not** depend on the bands: the Dojo scorer returns 1.0 for some truncated names
  (e.g. one token of a two-token person name). The bands cannot fix it — note it as an upstream finding if you see it.
- `date` and `id` are already 100%; `money` ≈ 99.6%. Real headroom is `entity_list` and `free_text` escalation rates.
- Contract and merger_agreement have **no** populated field labels in corpus v9.3 (they arrive with the v10 labeling
  campaign), so those classes are not represented.
- Changing taxonomy values needs a watcher/API restart in production (config is `lru_cache`d); not relevant to the loop.

## Strategy
1. (iters 1–5) one type at a time: narrow `entity_list` and `free_text` bands from the edges (0.5→0.6→0.7, 0.95→0.9), watch the guard.
2. (6–20) `bipartite_match_threshold`, `embedding_rescue_below`, then `money`/`name` edges; try asymmetric bands (move only low or only high).
3. (21–30) combine the best keeps; check that each keep still holds with the others applied.
Do NOT: change several unrelated keys at once; spend >3 iterations on one idea; touch `date`/`id` bands.

## Loop rules
- Commit each keep: `autoresearch: <what> | score: A -> B | guard: pass`. Revert a miss with
  `git checkout -- src/config/taxonomy.yaml`.
- Log every iteration to `autoresearch.jsonl` (format below) and regenerate `autoresearch_dashboard.md`.
  Statuses: `baseline|keep|discard|crash|guard_fail`.
- 30 iterations; each should take seconds. If one takes >2 minutes, stop and report.
- If you lose context, read `autoresearch.jsonl` and `autoresearch_dashboard.md`, continue from the last iteration.
```json
{"type":"result","iteration":N,"commit":"sha","score":0.0,"delta":"+0.000","guard_score":null,"guard_pass":true,"status":"keep","description":"…","timestamp":"ISO8601"}
```

## When done
Write a summary to the dashboard: start/final score, kept changes, what failed, whether the score plateaued (and if so,
that the ceiling is the scorer, not the bands), and a recommendation. Do not push; do not edit CHANGELOG. A human decides
whether any band change ships, with real-model evidence.
