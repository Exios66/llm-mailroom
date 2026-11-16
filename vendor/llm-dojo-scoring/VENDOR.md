# Vendored llm-dojo-scoring (fallback only)

Authoritative source: https://github.com/Exios66/llm-dojo-scoring

- Exact source commit: `13adc8ec722f0d635466062b6dda181958d4d5f8` (the Dojo `v0.30.0` branch head after llm-dojo-scoring#163, which carries #158; supersedes `93eedd8`, the 0.30.0 cut plus #157. Same public surface; scores move: the sixth 0.30.0 score-change set, `rescore_required` in `metrics/score_changelog.py`)
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

Whether a release has been cut on this commit is deliberately not recorded here: a tag can land
on the unchanged commit and nothing regenerates this file. The pin comment in `pyproject.toml`
owns that fact, and `src/scripts/bump_dojo_scoring.py` keeps it current.
