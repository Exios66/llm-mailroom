---
name: dojo-bump
description: Check whether llm-mailroom's llm-dojo-scoring pin is behind the latest Dojo release and, when asked, bump it and verify the suite against the real pinned version. Use only when the user asks to check, bump or update the Dojo pin.
disable-model-invocation: true
---

# dojo-bump

Mailroom consumes `llm-dojo-scoring` as a **git pin in `pyproject.toml`**: always a 40-hex
commit (`src/tests/test_dojo_source.py` requires it). `--tag vX.Y.Z` pins that release's
commit and labels the docs with the tag; while a fix is unreleased the pin is an interim SHA. The pin moves at Dojo release
time, or to an interim SHA before a release, always via `src/scripts/bump_dojo_scoring.py`.
**Never delete the pin line.**

## 1. Check (read-only)

```bash
PYTHONPATH=src .venv/bin/python src/scripts/bump_dojo_scoring.py --check
```

Prints `current=`, `desired=`, `up_to_date=`. Exit **0** = up to date, **2** = behind.
An interim SHA pin that the latest tag does not contain is reported up to date on
purpose — the auto-bump never downgrades it.

Stop here if the user only asked for status.

## 2. Bump (only when asked)

```bash
PYTHONPATH=src .venv/bin/python src/scripts/bump_dojo_scoring.py --apply --dry-run [--tag vX.Y.Z]
PYTHONPATH=src .venv/bin/python src/scripts/bump_dojo_scoring.py --apply [--tag vX.Y.Z]
```

Pass the same `--tag` to both, so the dry run previews the target you will apply (without
it, both use the latest GitHub release). For an **interim SHA** that no published release
contains yet, pass the 40-hex commit as `--tag` and add `--allow-missing-release`; without
that flag the script refuses an unreleased SHA. The flag also refuses to replace a SHA pin
with a tag, so moving from an interim SHA to a release needs the online check (no flag).

It rewrites the pin plus the documented pin references. Confirm the dry-run plan with
the user before applying. The daily `.github/workflows/bump-dojo-scoring.yml` does the
same by opening a PR, so check for an existing bump PR before doing it by hand.

## 3. Verify against the REAL pin

A local editable install of Dojo masks the pin and has produced false results here.
Install the exact pinned revision first:

```bash
.venv/bin/pip install --no-deps --force-reinstall \
  "llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@<sha-from-pyproject>"
.venv/bin/pip list | grep -i dojo
.venv/bin/python -m pytest src/tests -q          # about 5 minutes
```

`test_wheel_runtime_catalog` shells out to `uv build` and needs network; if it is the
only failure in an offline container, say so rather than calling the suite red.

## 4. Commit

`chore(deps): bump llm-dojo-scoring to <ref>` with the old and new pins and the test
result. Do not edit Digital-Mailroom or the sandbox from here — the sandbox refreshes its
own vendor snapshot (`/vendor-sync` in that repo).
