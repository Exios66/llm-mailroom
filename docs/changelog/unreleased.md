---
tags:
  - fix
---

# Unreleased

{% hint style="info" %}
This GitBook Changelog space is generated from the repository
[`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (Keep a Changelog). Do not hand-edit these
pages. Regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
{% endhint %}

Unreleased work on `main`. Canonical source: [`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md).

## Fixed

- **Failed extraction chunks no longer archive silently, and chunk confidence is the weakest window's.** `BaseAgent.extract_chunked` (corporate record, correspondence, insurance) and the vendored `_SpecialistBase.extract_chunked` (contracts, merger) swallowed every per-chunk error and merged confidence as the MAX, so four windows `[0.98, ConnectionError, parse error, 0.4]` returned 0.98 with partial keys and auto-archived without a judge. A transient provider error in a window is now re-raised, so the node's `transient_error` routing retries it. A window that fails to parse (or raises a non-transient error) is counted: the result carries `_n_chunks` / `_failed_chunks` (underscore keys, ignored as non-public) and its confidence is capped strictly below the class `low` threshold, so routing retries or escalates. Merged confidence over the surviving windows is now the MIN, not the MAX. Single-window documents are unchanged. **Comparability:** long documents whose windows differ in confidence now score lower, and more of them reach retry or judge.
- **Sorter window disagreement now lowers confidence.** `_merge_sorter_reads` averaged only the windows that agreed with the winning class, so `[contract .99, corporate_record .99, correspondence .99]` merged to `contract .99`, and one `contract` window beside four `unknown` windows did too. Confidence is now the agreeing windows' mean times the share of non-unknown windows that agree (the first case reads 0.33), and when the winner holds fewer than half of all windows (unknown included) it is also capped strictly below its class `low` threshold. The vote vector rides the merged read as `_window_votes`. Single-window documents are unchanged. **Comparability:** over-budget documents with split window reads now reach retry or review where they used to continue.
- **A matter conflict no longer bypasses the extraction quality gates.** `after_extraction` sent `conflict_detected` straight to `boss_escalation` before the schema, hollow, coverage and confidence checks, and `after_boss` "approved" goes to `compile_report`, so a conflicted extraction with confidence 0.1 and an invalid schema was approved and archived with no retry, judge or review. The conflict check now runs at the point a sound extraction (schema valid, not hollow, coverage met, confidence at or above `low`) would otherwise go to `compile_report`; a failing extraction retries or escalates exactly as it would without a conflict. Return types and edge maps are unchanged.
- **Matter conflict detection no longer fires on formatting differences.** `_detect_conflict` compared normalized text for exact equality, so `Delaware` vs `State of Delaware` and `2024-01-05` vs `January 5, 2024` escalated to the Boss. Complete dates now compare as calendar dates (a bare year or month is not a date, and an ambiguous `01/02/2024` stays a text comparison); jurisdiction-like fields (`governing_law`, `jurisdiction`, `dispute_forum`, `venue`, court) ignore filler words (`state of`, `the`, `commonwealth`, `laws`) but still differ on any name part, so `Virginia` vs `West Virginia` and `State of New York` vs `State of New Jersey` remain conflicts; list fields are equal when one normalized set contains the other. The Dojo's `score_field` was tried and rejected for this: it scores `State of New York` vs `State of New Jersey` at 0.92, which a 0.9 cutoff would have hidden.
- **`_transient_decision` no longer claims the transient counters reset on success.** They never did, and must not: `first_pass_success` reads `transient_retries_<node>` to keep a document that needed a provider retry out of the first-pass KPI. The docstring now says so and a test pins it. Known limit: the budget is cumulative per node per run, so a node revisited several times (`retry_extract` through the arbiter loop) shares one budget of 2 transient retries.
