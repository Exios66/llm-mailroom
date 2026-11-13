# Vendored llm-dojo-scoring (fallback only)

Authoritative source: https://github.com/Exios66/llm-dojo-scoring

- Exact source commit: `f4f7e92d82956d48dad5976e40e321befc57e03d` (untagged interim pin, after `v0.28.1`: llm-dojo-scoring#136 production slim-down + #138 CUAD per-category counts + #139 scoring fixes D1-D4)
- Source package version: `0.28.1` (`__version__` at that commit; the code is ahead of the `v0.28.1` tag)
- Snapshot: `git archive` of `llm_dojo_scoring/` at that commit, relocated under `src/`.
  That includes the package data `schemas/data/extraction_status.json`, which `.gitignore`
  re-includes explicitly (its `data/` rule had dropped it from earlier snapshots).

The installed release (the pyproject git pin) is the primary Dojo. This snapshot is the
backup that `src/dojo_source` serves only when the installed release is missing or fails to
import, so scoring survives a broken install or a dependency outage. Set
`MAILROOM_DOJO_SOURCE=vendored` to force it, `installed` to disable the fallback.

Never patch this tree. When the pin moves (`src/scripts/bump_dojo_scoring.py`), replace the
snapshot with the new commit's tree and update the commit and version recorded here;
`src/tests/test_dojo_source.py` fails while the commit recorded in this file differs from the
pyproject pin, or the version recorded here differs from the snapshot's `__version__`. It does
not compare the rest of the snapshot with that commit.
