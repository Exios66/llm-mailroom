## Problem

<!-- The request, issue or task this PR answers. Link it. Quote the original ask if it came from a person. -->

## Change

<!-- What changed and why this approach. One concern per PR. -->

## Evidence

<!--
Proof that the change solves the problem above, taken on the final commit.
See .claude/rules/evidence-contract.md. Quote real output; trim with … ; long output in <details>.
-->

Commit: `<short sha>`

- **Tests:** `.venv/bin/python -m pytest src/tests -q` → `<summary line>`
- **Regression test (bug fixes):** `<test name>` fails before, passes after
- **Pipeline gates (when behavior changed):** `run_pilot.py --mock` → `<summary>`; experiment gate → `<verdict>`
- **Static checks:** `.venv/bin/python -m compileall -q src` → `<observed exit code>`. No linter or type checker is configured in this repo.
- **Generated files:** `<PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py --check, PYTHONPATH=src .venv/bin/python src/scripts/export_contract.py --check, or "not applicable">`
- **Visual (UI changes):** `<before / after screenshots or recording with synthetic or redacted data, or "not applicable">`

<details><summary>Raw output</summary>

```text
```

</details>

## Not verified

<!-- What you could not check and why (network-only tests, real-LLM paths, surfaces you could not render). "Nothing" is a claim too. -->

## Review log

<!-- Adversarial rounds (count, lens, findings, fixes) and the fresh pre-PR review verdict. See .claude/rules/orchestration.md. -->

- Adversarial review: `<N>` round(s) — `<summary>`
- Pre-PR review (fresh context): `<READY / fixes applied>`

## Cross-repo impact

<!-- llm-dojo-scoring, local-mailroom-sandbox, The-Mailroom, Digital-Mailroom: what needs a follow-up, or "none" and why. -->

## Checklist

- [ ] Evidence above was taken on the final commit
- [ ] Every behavior change has a test; no test was skipped or loosened to get a pass; existing conditional skips are disclosed
- [ ] `CHANGELOG.md` `[Unreleased]`, docs, `AGENTS.md` and `.env.example` updated where behavior changed
- [ ] No hand-edits to `docs/changelog/`, the experiment baseline, `data/` or `.env`
- [ ] No real-LLM or hosted-service writes were run without the owner's go-ahead
