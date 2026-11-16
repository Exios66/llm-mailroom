# Vendored llm-dojo-scoring (fallback only)

Authoritative source: https://github.com/Exios66/llm-dojo-scoring

- Exact source commit: `116da0b0f5104165244ec9a4b7f068c1cf0048cf` (untagged: the Dojo `v0.30.0` merge of PR #169, which fixes llm-dojo-scoring#168 (a typed decision is never graded by an untyped score when its class is missing) on top of `1dc9a09`, the merge of PR #166 with the correspondence `production_prompts` v8 correction for llm-mailroom#269 and the #163 follow-ups; no GitHub release exists yet)
- Source package version: `0.30.0` (`__version__` at that commit)
- Snapshot: `git archive` of `llm_dojo_scoring/` at that commit, relocated under `src/`.
  That includes the package data `schemas/data/extraction_status.json`, `ml/data/ml_contract.json`
  and `prompts/data/alignment_gaps.json`, which `.gitignore` re-includes explicitly (its `data/`
  rule would otherwise drop them). The tracked file list equals `git ls-tree -r` of
  `llm_dojo_scoring/` at that commit (131 files).

The installed release (the pyproject git pin) is the primary Dojo. This snapshot is the
backup that `src/dojo_source` serves only when the installed release is missing or fails to
import, so scoring survives a broken install or a dependency outage. Set
`MAILROOM_DOJO_SOURCE=vendored` to force it, `installed` to disable the fallback.

Never patch this tree. When the pin moves (`src/scripts/bump_dojo_scoring.py`), replace the
snapshot with the new commit's tree and update the commit and version recorded here;
`src/tests/test_dojo_source.py` fails while the commit recorded in this file differs from the
pyproject pin, or the version recorded here differs from the snapshot's `__version__`. It does
not compare the rest of the snapshot with that commit.
