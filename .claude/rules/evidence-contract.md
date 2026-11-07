# Evidence contract

**Nothing is "done", "fixed", "passing" or "working" until you hold evidence for it.**
This applies to the orchestrator, to every subagent report, to commit messages and to
PR descriptions. A claim without evidence is a guess; label it as one or drop it.

## Rules

1. **Evidence is output, not prose.** Quote the command, its exit code and the lines that
   prove the claim (the pytest summary line, the gate verdict, the screenshot). "Should
   work", "looks right" and "I believe" are not evidence.
2. **Evidence comes from the final state.** Commit the last edit, then re-run the gates on a clean
   tree (`git status --short` prints nothing). Name that commit
   (`git rev-parse --short HEAD`). Output from before a later change is stale.
3. **Prove the original problem is solved, not just that nothing broke.** A green suite
   shows no regression; it does not show the fix works. For a bug, show a test that fails
   before the fix and passes after (red, then green). For a feature, show it doing the
   thing the user asked for.
4. **Say what you did not verify, and why.** Skipped tests, network-only checks, a real-LLM
   path you were not allowed to run, a surface you could not render. Never present a
   partial run (`affected_tests.py`, one `-k` filter) as the full suite.
5. **Re-verify subagent evidence.** The orchestrator re-runs at least the verification
   gates itself (or reads a `runner`'s raw output) before repeating any subagent claim.
6. **Never fabricate or "tidy" output.** Trimming is fine (mark it `…`); editing numbers,
   dropping failures or merging runs is not.

## What each kind of claim needs

| Claim | Evidence |
|:---|:---|
| Tests pass | `.venv/bin/python -m pytest src/tests -q` summary line (`N passed, M skipped …`), exit code, commit. Name every skip that matters and every environment-only failure (`test_wheel_runtime_catalog` with an empty uv build cache). |
| Bug fixed | The new or changed regression test, its failing output on the old code, its passing output on the new code. |
| Pipeline behavior unchanged / improved | `PYTHONPATH=src .venv/bin/python src/scripts/run_pilot.py --mock` summary, and the experiment gate: `PYTHONPATH=src .venv/bin/python src/scripts/run_experiment.py --dataset mailroom-fixtures --mock --baseline docs/superpowers/baselines/experiment-baseline.json` verdict. |
| Lint / static checks | **No linter or type checker is configured in this repo, by design. Do not add one.** Report `.venv/bin/python -m compileall -q src` (exit 0) and say "no linter or type checker configured". Never claim "lint clean" or "type-safe". |
| Generated files current | `PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py --check`, `PYTHONPATH=src .venv/bin/python src/scripts/export_contract.py --check` when graph, agents, classes, scores or thresholds changed, `python3 src/scripts/release.py check` for release work. |
| Docs are true | `src/tests/test_docs_truth.py` passes; every command you added was run (paste the result) or is marked unverified; every path you named exists. |
| UI / visual change (`landing/`, `tree.html`, mascot, notebook figures, README images) | Before and after screenshots (Playwright, `deviceScaleFactor: 2`). For an interaction, a short recording (Playwright `recordVideo`) or GIF. Plus `src/tests/test_landing.py` for landing changes. |
| Config / taxonomy change | The affected tests, `PYTHONPATH=src .venv/bin/python src/scripts/cutover.py --validate --agent <name>` for model mapping changes, and a note that the watcher/API needs a restart. |
| Dojo pin bump | `bump_dojo_scoring.py --check` before and after, and the suite run against the **real pinned revision** (see the `/dojo-bump` skill). |
| Performance / cost | Numbers before and after, with the method and the sample size. |
| Cross-repo impact | Which sibling repo is affected (see `CLAUDE.md`), and whether it needs a follow-up change. "None" is a claim too: say why. |

## Where evidence goes

- **Text** (test summaries, gate verdicts, command output) goes in the PR description
  under "Evidence", inside `<details>` blocks for anything longer than ten lines. Use the
  template in `.github/pull_request_template.md`.
- **Screenshots and recordings:** save them under `.evidence/` (git-ignored); never commit
  binaries to the feature branch. Push them to the orphan branch `pr-evidence`, under
  `<feature-branch>/`, and embed them with
  `![what it shows](https://github.com/Exios66/llm-mailroom/blob/pr-evidence/<feature-branch>/<file>.png?raw=true)`.
  That branch is never merged; pushing evidence files there is allowed. If the push is
  refused (cloud sessions are often limited to one branch), send the files to the user
  and list each file and what it shows under "Visual evidence" in the PR, marked
  "attached by hand". Never describe a screenshot you did not take.
- **Keep captures free of real documents.** This repo and its `pr-evidence` branch are
  public. Capture synthetic or sample data only, never real or client documents, names,
  tokens or local paths. If a surface cannot be shown that way, redact it and check the
  final image, or leave it out and say so under "Not verified". This covers files attached
  by hand too.
- **Final report to the user:** the same evidence, short. Lead with what was verified and
  what was not.
