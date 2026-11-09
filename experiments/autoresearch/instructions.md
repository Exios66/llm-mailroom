# Autoresearch loop protocol

One loop per objective in `objectives.yaml` (`thresholds`, `scale`, `review`). Every
objective names its **metric**, its **direction**, the **guards** an iteration must pass,
the **evidence** a kept change needs and what does **not** count as evidence.

## One iteration

1. Change only the objective's `constrained_file`. The candidate policy YAML is the
   change for `thresholds`, and it never edits `src/config/taxonomy.yaml`.
2. Commit the change locally, so the gate records a clean commit and not `-dirty`.
3. Run the gate:

   ```bash
   PYTHONPATH=src .venv/bin/python src/scripts/autoresearch_gate.py \
       --objective <name> --description "<what changed and why>" \
       [--candidate <policy.yaml> --records <test.jsonl> --baseline <current.yaml>]
   ```

4. Act on the exit code:

   | Exit | Meaning | What to do |
   |:--|:--|:--|
   | 0 | `keep`, `baseline` or `pass` | Keep the commit. |
   | 3 | `discard` | Every guard passed, but the gain was below `min_delta`. `git revert` the change. |
   | 2 | `guard_fail` | A core guard, an eval `GUARD`/`GATE` line or the ΔU interval failed. Revert the change. |
   | 4 | `harness` | The records are not evidence (mock/synthetic modes); regenerate them in real mode (owner-run) before scoring. |
   | 1 | `crash` | The eval did not run. Fix the harness before you score anything. |

The gate runs the **core guards first**: compileall, the affected tests, the hermetic
experiment gate, the display contract and the changelog check. A change that breaks
core functionality is therefore discarded whatever its score. With `require_delta_ci`,
the first scored candidate must clear `min_delta` on its own ΔU point estimate before
it becomes the baseline the loop climbs from (a negligible-but-significant first gain
does not set the bar). Each result is appended to
`state/<objective>.jsonl` and summarised in `state/<objective>_dashboard.md`. That
directory is git-ignored, so copy the dashboard into the PR as evidence.

## Rules

- **Frozen:** these never change inside a loop.
  - `src/scripts/hillclimb_eval.py`, `src/pipeline/policy_replay.py` and
    `src/scripts/stress_test.py`;
  - the cost constants and guard limits;
  - the test split;
  - `objectives.yaml` itself.

  Changing the ruler to win the race is not an iteration.
- **Tune on train, score on test.** Never fit cutoffs on the records you score.
- **No real LLM call** without the owner. `--real` sweeps are owner-run with
  `MAILROOM_ALLOW_REAL=1`. Mock and synthetic records validate the harness only (see each
  objective's `not_evidence`) — the gate enforces this: an eval whose `records:` line
  reports non-real modes exits `4 harness` and can never baseline, keep, or seed the bar.
- **Promotion:** a kept change reaches `main` only through a reviewed PR, after the full
  suite has passed on the final commit (`.venv/bin/python -m pytest src/tests -q`).
