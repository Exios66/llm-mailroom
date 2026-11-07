# Subagent orchestration

These rules govern how the main session delegates work. They apply to every task in
this repository. `AGENTS.md` points other agent tools here; map the tiers to the
closest models your tool offers.

## Roles and model tiers

| Role | Who | Model / effort | Use for |
|:---|:---|:---|:---|
| Orchestrator | The main session | Opus | Plans the work, writes the subagent briefs, owns the final verification, talks to the user. |
| `implementer` | `.claude/agents/implementer.md` | `sonnet`, effort `high` | Scoped, higher-level work: a feature, a bug fix with root-cause analysis, a refactor, a design or investigation write-up. |
| `runner` | `.claude/agents/runner.md` | `haiku`, effort `high` | Simple tasks: fetch data, run commands or test suites, collect results, read logs, simple mechanical edits (rename, add a line, fix a typo). |
| `adversarial-reviewer` | `.claude/agents/adversarial-reviewer.md` | `sonnet`, effort `high` | Tries to break subagent output before the orchestrator accepts it. Read-only. |
| `pre-pr-reviewer` | `.claude/agents/pre-pr-reviewer.md` | `sonnet`, effort `high` | Fresh-context review of the final diff before anything is pushed for a PR. Read-only. |

**Hard rules**

1. **YOU MUST NOT start an Opus subagent.** Opus orchestrates; it does not spawn copies
   of itself. A subagent with no `model` inherits the main session's model, so:
   - prefer the project agents above (their frontmatter pins `model` and `effort`);
   - when you use a built-in type (`general-purpose`, `Explore`, `Plan`), **always** pass
     `model: "sonnet"` or `model: "haiku"` and `effort: "high"` explicitly;
   - a `fork` always runs on the main session's model, so do not fork for delegated work.
2. Pick the **cheapest tier that can do the job well**. If a task needs judgement
   (choosing an approach, reading unfamiliar code, explaining *why*), it is `implementer`
   work, not `runner` work. If the `runner` reports that the task was not simple, re-brief
   it to an `implementer`; do not retry the `runner` with the same brief.
3. Do not delegate trivial lookups you can answer with one `Read` or `Grep`. Every
   subagent starts cold and re-reads context, so delegation has a cost.
4. Run independent subagents in parallel (one message, several `Agent` calls). Run
   dependent ones in sequence.
5. Subagents never push, open PRs, comment on GitHub, publish, or run anything that spends
   money or writes to hosted services. Those actions stay with the orchestrator.

## Writing a brief

A subagent sees only its brief, not this conversation. Scope and prepare every brief
for the circumstances. Include:

- **Goal:** the user's original request, quoted, plus the slice this subagent owns.
- **Context it cannot discover cheaply:** files and functions involved, decisions already
  made, constraints from `CLAUDE.md` that apply, what was already tried.
- **Boundaries:** files it may change, files it must not touch, commands it must not run.
- **Done means:** the acceptance check and the evidence it must return
  (see `.claude/rules/evidence-contract.md`).
- **Return format:** what changed (paths), commands run with exit codes and the key output
  lines, what it could not verify, open questions.

## Adversarial review (required)

Every change produced by subagents gets at least **one** adversarial review round before
the orchestrator verifies it, and at most **three**. Size the rounds to the work:

| Rounds | When |
|:---|:---|
| 1 | Up to about 100 changed lines in one area, including docs-only, test-only or mechanical changes of that size. |
| 2 | About 100 to 500 changed lines, several modules, or any behavior change. |
| 3 | More than about 500 lines; or a public API, cross-repo contract, scoring or metric change, release, migration, security- or secrets-sensitive change. |

Commit the work first, then count lines with `git diff --stat <base>...HEAD`, which does
not see uncommitted files. Commit again before every later round. Size and risk both count: a large
docs-only change is not tier 1. When in doubt, pick the higher tier.

Each round is a **new** `adversarial-reviewer` instance (never resume a reviewer with
`SendMessage`), and each round has its own lens:

1. **Does it solve the problem?** Compare the result with the original request. Re-run
   the claimed evidence. Look for requirements that were skipped or quietly narrowed.
2. **What breaks?** Edge cases, error paths, regressions in callers, cross-repo
   contracts, config and docs that the change made stale.
3. **Try to break it.** Hostile inputs, concurrency, partial failure, the claims in the
   PR description and docs, anything that looks too easy.

Between rounds, send BLOCKING and MAJOR findings back to an `implementer` (or fix them
yourself if trivial), then start the next round on the updated diff. If BLOCKING
findings remain after round 3, stop and report to the user; do not start a fourth round.
A BLOCKING or MAJOR finding from the last round you run gets the same treatment: fix and
re-verify it, or report it to the user. Never carry one silently into a PR.
Record every round (lens, findings, what was fixed) for the PR's "Review log".

## Orchestrator verification (required)

Subagent reports are claims, not evidence. Before you tell the user anything is done:

- read the full diff yourself;
- re-run the verification gates from `CLAUDE.md` on the final state of the branch
  (delegating the run to a `runner` is fine; reading its raw output is not optional);
- check each item of `.claude/rules/evidence-contract.md` against what you hold.

If the user asked for a push or a pull request, follow `.claude/rules/pr-flow.md`. If not,
leave the change local and report it.
