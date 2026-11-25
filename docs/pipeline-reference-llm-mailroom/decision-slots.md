# Decision-model slots

Eight places in the graph where a decision model may change what happens to a document.
They are configured in `src/config/taxonomy.yaml` under `decision_slots:` and implemented
in `src/graph/decision_slots.py`. Every slot ships `mode: baseline`: the pipeline then runs
exactly as it would without the block (no node is wrapped, no route rule is registered,
no slot state key is written, no slot calls a model). A registered extraction gate still
answers on each extraction attempt either way, and its record carries the `attempt` it judged.

The mailroom never builds a decision model. The host registers one per role:

| Role | Registry | Used by |
| --- | --- | --- |
| Sorter reviewer | `agents.decision_backend.register_decision_backend` | `sorter_reviewer` |
| Extraction gate | `agents.extraction_gate.register_extraction_gate` | `archive_gate`, `judge_replacement`, `arbiter_veto`, `judge_check`, `boss_check`, `field_review` |
| Closed-field resolver | `agents.closed_field_resolver.register_closed_field_resolver` | `closed_field_resolver`, `field_review` |

The experiments run these on OpenRouter Decisions-API models (local-mailroom-sandbox
registers them from its run spec); the chat sorter reviewer is the baseline.

## The slots

| Slot | Baseline | Decision-model role | Guard |
| --- | --- | --- | --- |
| `sorter_reviewer` | `reviewer:` decision_only classes park on any failed rule | `park_cap`: past it the chat reviewer reviews instead | accepts are never capped; every non-accept route (a park or a classification-arbiter escalation) counts |
| `archive_gate` | gate answer recorded only | `enforce`: the gate parks an archive it does not support, on the `extract`, `retry_extract` and `judge_verify` routes; optional `release` of a soft park | `park_cap` 10%, `release_cap` 3% |
| `judge_replacement` | LLM judge, up to `judge_max_passes` | `gate`: the gate answer on this attempt is the verdict (classes with `gate_key_fields` only); no judge call, no judge score | an arbiter `accept_with_caveats` on a gate `incomplete` always parks (`gate_incomplete`, never capped) |
| `arbiter_veto` | `accept_with_caveats` parks unless `confidence.arbiter_accept_archives` | `enforce`: gate disagreement parks (`arbiter_veto_gate`); `on_unverified: park` also parks an accept the gate cannot confirm (`arbiter_accept_unverified`) | only parks, never releases; `park_cap` (none by default); no charge when `judge_replacement` already parks |
| `judge_check` | a judge `complete` parks unless `confidence.judge_complete_archives` | `enforce`: where a judge `complete` would archive, gate disagreement parks (`judge_check_unsupported`); `on_unverified: park` also parks a `complete` the gate cannot confirm (`judge_complete_unverified`) | only parks; `park_cap` 10%; no charge when `archive_gate` already parks |
| `boss_check` | a Boss `approved` archives (a blind one parks, `boss_ungrounded`) | `enforce`: gate disagreement with a Boss approval parks (`boss_check_unsupported`); `on_unverified: park` also parks one the gate cannot confirm (`boss_approval_unverified`) | only parks; `park_cap` 10% |
| `closed_field_resolver` | off-list closed label parks (#285) | `suggest` records an allowed pick; `resolve` applies it | every off-list field needs an on-list pick at `min_probability`; `release_cap` 3% |
| `field_review` | the whole document parks | `annotate`: the review block lists `review_focus` fields | at most `max_fields`, else the whole document |

### The gate verdict is derived in code

The gate answers one "is this value supported by the source" question per key field
(`field_support`, probabilities). The slots never read the model's own `verdict` answer:
a field below `gate_support_min` (default 0.5) is unsupported, and an extraction with any
unsupported field is not supported. The answer counts only for the extraction attempt it
judged (`extraction_gate.attempt` equals `extraction_attempts`); an error, a skip, a stale
or malformed answer is `unavailable`, and the slot then follows the baseline, except that
`arbiter_veto`, `judge_check` and `boss_check` with `on_unverified: park` park the approval
instead (within `park_cap`).

### Key fields

`gate_key_fields` names, per class, the fields the gate must answer. It is the mailroom's own
list (the shipped one matches the sandbox gate question set `extraction_gate_v1.3.0`), so
coverage never rests on what the plug-in says it asked: a key field without an answer is
support 0. Field names the gate returns that are not fields of the class's extraction schema
are dropped, and at most 64 are read, so plug-in text never reaches findings or review
evidence. A class without key fields can still park through `archive_gate` and
`arbiter_veto`, but is never released and never has its judge replaced.

### Releases are narrow

`archive_gate.release: true` lets the gate archive a document the router would park, only
when all of these hold:

- every review cause is soft: `low_extraction_confidence`, `retries_exhausted`, `judge_partial`,
  `judge_complete_unconfirmed` (a judge `complete` the default archive checks parked);
- the judge did not say `incomplete` (or error), there is no matter conflict and no guardrail issue;
- the class has `gate_key_fields` and every one of them is supported at `release_support_min`
  (default 0.9); a key field the gate did not answer counts as support 0;
- the `release_cap` allows it.

`archive_gate` does not see an arbiter `accept_with_caveats` (that is `arbiter_veto`'s
place, and an accept is never released) or a Boss approval (`boss_check`'s place).

### The default archive checks and the slots

Without any slot, taxonomy `confidence` keeps a judge `complete`, an arbiter
`accept_with_caveats`, an unjudged retry of an extraction the judge doubted
(`retry_unjudged`) and a blind Boss approval from archiving (see the configuration
reference). The slots work on top of that: `archive_gate.release` is the capped way a
decision model may archive a judge `complete` it backs on every key field, and
`judge_check` / `arbiter_veto` matter for a class that sets `judge_complete_archives` or
`arbiter_accept_archives` back to `true`.

## Caps

A cap is a share of the documents the slot has seen in this process. A park or release
is allowed while `count + 1 <= cap * max(seen, cap_warmup)`; `cap_warmup` (default 20)
keeps a cap from opening wide on the first documents, so a 3% release cap allows no release
before document 34. Each document counts once per slot. A capped decision follows the
baseline route and is recorded with `action: capped`. A decision reviewer park caused by its
backend failing (`reviewer_unavailable`) never spends the `sorter_reviewer` cap. The ledger is per process: each API
or watcher worker keeps its own. `graph.decision_slots.reset_ledger()` starts a new one and
`ledger_snapshot()` reports it.

## What a run records

- `decision_slot_outcomes[slot]`: the slot's latest decision. `node`, `attempt`,
  `proposal` (what the router chose), `action` (`park`, `release`, `capped`, `agree`,
  `hold`, `unavailable`, `replaced`), `route`, `cause`, and the gate numbers
  (`unsupported_fields`, `min_support`, or `gate_reason`).
- `decision_slot_marks`: `seen:<slot>`, `park:<slot>`, `release:<slot>` for the document.
  The outcomes are cleared when a person resolves a park; the marks stay.
- `closed_field_resolutions`: each off-list field, its original value, the pick, its
  probability and whether it was applied. An applied pick also leaves
  `extracted_data["_closed_field_resolutions"]` on the archived record.
- `judge_source`: `extraction_gate` or `llm` while `judge_replacement` is on.
- Review causes `archive_gate_unsupported`, `arbiter_veto_gate`, `gate_incomplete`,
  `arbiter_accept_unverified`, `judge_check_unsupported`, `judge_complete_unverified`,
  `boss_check_unsupported`, `boss_approval_unverified`.
- A parked document's `_review.review_focus` under `field_review: annotate`.

## Example (Set B: enforcing gate, judge replaced)

Change only these entries of the shipped block and keep its `gate_key_fields`: without key
fields no class has coverage, so the judge is never replaced and nothing releases.

```yaml
decision_slots:
  archive_gate: {mode: enforce, park_cap: 0.10}
  judge_replacement: {mode: gate}
  arbiter_veto: {mode: enforce}
```

Edits need a watcher or API restart (the taxonomy is cached). A malformed block stops the
watcher and the API at startup.
