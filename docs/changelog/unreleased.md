---
tags:
  - improvement
  - fix
---

# Unreleased

{% hint style="info" %}
This GitBook Changelog space is generated from the repository
[`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (Keep a Changelog). Do not hand-edit these
pages. Regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
{% endhint %}

Unreleased work on `main`. Canonical source: [`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md).

## Changed

- **Dojo pin → v0.27.2** (`1cd4aba`, llm-dojo-scoring#121), from the 0.27.1 pin `a82843e`. The pin stays an exact commit (the `v0.27.2` tag's commit). 0.27.2 is a fix release:
  - `score_document` never raises on deeply nested prediction text, and parses brace-heavy text in linear time.
  - Any `Mapping` prediction counts as a dict payload for schema adherence.
  - A prose-wrapped Python repr sets `prose_wrapper`.
  - `rescore` reports the CUAD/MAUD `headline_score` as the extraction score.

  Field-map, tier 0 and headline scores are unchanged. Moved with `bump_dojo_scoring.py --apply --tag 1cd4aba…`.

## Fixed

- **Vendored Dojo fallback was missing its package data.** `.gitignore`'s `data/` rule dropped `schemas/data/extraction_status.json` from `vendor/llm-dojo-scoring`, so under the fallback (`MAILROOM_DOJO_SOURCE=vendored`, or a broken install) `extraction_schema()` raised `FileNotFoundError`. The 0.27.2 snapshot includes it, and `.gitignore` re-includes that directory.
