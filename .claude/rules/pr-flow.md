# Pull request flow

Bot reviewers (CodeRabbit, CodeAnt) should spend their attention on real bugs, not on
mistakes we could have caught ourselves. So **nothing is pushed for review until it has
been vetted locally and by a fresh-context reviewer.** Once the user has asked for a push
or a pull request, run these steps in order.

## 1. Local gate (before any push)

On the final state of the branch, run the gates that match the change (see "Verification
gates" in `CLAUDE.md` and `.claude/rules/evidence-contract.md`) and keep the raw output.
At minimum:

```bash
git status --short                      # prints nothing: commit the work first
git diff origin/main...HEAD --stat      # the diff is the size you expect
.venv/bin/python -m compileall -q src   # syntax only; no linter is configured
PYTHONPATH=src .venv/bin/python src/scripts/affected_tests.py --run -- -q   # fast loop
.venv/bin/python -m pytest src/tests -q # full suite before the PR is marked ready
```

Add the generated-file checks when they apply
(`PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py --check`,
`PYTHONPATH=src .venv/bin/python src/scripts/export_contract.py --check`). A red gate stops the flow; fix it first.

## 2. Self-review checklist

Read your own diff top to bottom before handing it to a reviewer:

- No debug prints, commented-out code, stray TODOs, local paths, secrets or `.env` values.
- No unrelated changes (formatting churn, drive-by renames). One concern per PR.
- Every behavior change has a test; every bug fix has a regression test.
- Docs, `AGENTS.md`, `CHANGELOG.md` (`[Unreleased]`), `.env.example` and docstrings match
  the new behavior. Counts, versions and paths named in prose are still true.
- Names, error messages and log events are consistent with neighbouring code.
- Cross-repo effects are noted (see "Constellation" in `CLAUDE.md`).

## 3. Fresh-context pre-PR review (required)

Dispatch a **new** `pre-pr-reviewer` subagent for every PR, and a new one again after
any non-trivial fix. Never reuse a reviewer that saw the implementation (no `SendMessage`
to an earlier reviewer, no fork): its context is the thing we are trying to avoid.

Give it only:

- the user's original request, quoted;
- the base ref and branch name (`origin/main...HEAD`);
- the draft PR description with the evidence section, leaving out its "Review log";
- the gate commands you ran and their exit codes.

Do **not** give it the implementer's reasoning or the adversarial-review history; it must
judge the diff as a stranger would. Fix every BLOCKING and MAJOR finding, then re-run the
local gate. If the fixes were more than trivial, dispatch another fresh `pre-pr-reviewer`
(at most two passes; then escalate to the user).

## 4. Push and open a draft PR

- Push the designated branch only (`git push -u origin <branch>`). Never force-push a
  shared branch.
- Open the PR **as a draft**, filled in from `.github/pull_request_template.md`: problem,
  change, evidence, what was not verified, review log, cross-repo impact.
- Mark it ready only when every evidence item is present and the full suite is green on
  the pushed commit.

## 5. Handling bot and human review

- Treat each comment as a claim to verify, not an order. Reproduce it against the code.
- **Valid:** fix it with a test where possible, push, and reply with what changed and
  the evidence.
- **Not valid:** reply with the file and line or the output that shows why. Do not
  "fix" correct code to quiet a bot.
- **Out of scope:** say so and propose a follow-up issue; do not grow the PR.
- After CI goes red, reproduce the failure locally first, then fix it. Never skip,
  disable or loosen a test to get green.

## 6. Close the loop

When a bot finds a careless mistake that steps 1 to 3 should have caught, add one line
to "Lessons from bot reviews" in `.claude/agents/pre-pr-reviewer.md` in the same PR, so
the next fresh reviewer checks for it.
