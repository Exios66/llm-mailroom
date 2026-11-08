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
