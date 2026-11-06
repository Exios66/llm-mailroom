---
tags:
  - feature
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

## Added

- **Display contract for The-Mailroom** (`contract/mailroom-contract.json`, `src/scripts/export_contract.py`).
  - Built from the pipeline's runtime constants: the compiled LangGraph's nodes and edges with each node's span and Langfuse observation type, `NODE_OBSERVATION_TYPES`, taxonomy agents, the five document classes with their schema, specialist, field keys and sorter subclass catalog, failure classes, `SCORE_CONFIGS` names and Langfuse aliases, and the `confidence` thresholds.
  - The-Mailroom will vendor the file at its pinned commit and test against it (in review there), so pipeline changes that break the visualizer fail in its CI instead of rendering as `unknown`.
  - `src/tests/test_contract_export.py` fails when the committed file is stale; regenerate with `PYTHONPATH=src python src/scripts/export_contract.py --write`.
  - `release.py cut` moves the contract's `pipeline_version` with the other version locations.
  - `traced_node` tags its wrapper with `mailroom_span_name` so the exporter can read each node's span from the graph.
  - `intake-ml-triage` is now listed in `NODE_OBSERVATION_TYPES` (type `span`, as emitted).

## Changed

- **Dojo pin → v0.27.2** (`1cd4aba`, llm-dojo-scoring#121), from the 0.27.1 pin `a82843e`. The pin stays an exact commit (the `v0.27.2` tag's commit). 0.27.2 is a fix release:
  - `score_document` never raises on deeply nested prediction text, and parses brace-heavy text in linear time.
  - Any `Mapping` prediction counts as a dict payload for schema adherence.
  - A prose-wrapped Python repr sets `prose_wrapper`.
  - `rescore` reports the CUAD/MAUD `headline_score` as the extraction score.

  Field-map, tier 0 and headline scores are unchanged. Moved with `bump_dojo_scoring.py --apply --tag 1cd4aba…`.

## Removed

- **Railway deployment.** `railway.json` and the Railway-only `nixpacks.toml` fallback are deleted (Railway stops honouring Config as Code on 2026-12-01; the owner retired the platform, The-Mailroom ADR-005). The hosted producer is the Hugging Face Docker Space paired with The-Mailroom Observatory. The deployment docs now describe the Space and generic container hosts.
  - The `auto` provider's local-Phoenix skip now keys on a Hugging Face Space (`SPACE_ID` / `HF_SPACE_ID`) instead of `RAILWAY_*`, with the same intent: hosted containers have no local collector.
  - The off-loopback bind error names Hugging Face Spaces instead of Railway.
  - `src/tests/test_railway_listen.py` is now `src/tests/test_listen_guards.py` and asserts that no Railway config comes back.

## Fixed

- **Vendored Dojo fallback was missing its package data.** `.gitignore`'s `data/` rule dropped `schemas/data/extraction_status.json` from `vendor/llm-dojo-scoring`, so under the fallback (`MAILROOM_DOJO_SOURCE=vendored`, or a broken install) `extraction_schema()` raised `FileNotFoundError`. The 0.27.2 snapshot includes it, and `.gitignore` re-includes that directory.
