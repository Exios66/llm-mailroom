# Vendored llm-dojo-scoring (fallback only)

Authoritative source: https://github.com/Exios66/llm-dojo-scoring

- Exact source commit: `2fea5ea7cff79d9e4c8f3513dd4a179089a8e8ff`
- Source package version: `0.26.0`
- Snapshot: `git archive` of `llm_dojo_scoring/` at that commit, relocated under `src/`.

The installed release (the pyproject git pin) is the primary Dojo. This snapshot is the
backup that `src/dojo_source` serves only when the installed release is missing or fails to
import, so scoring survives a broken install or a dependency outage. Set
`MAILROOM_DOJO_SOURCE=vendored` to force it, `installed` to disable the fallback.

Never patch this tree. When the pin moves (`src/scripts/bump_dojo_scoring.py`), replace the
snapshot with the new commit's tree; `src/tests/test_dojo_source.py` fails while the
snapshot and the pyproject pin disagree.
