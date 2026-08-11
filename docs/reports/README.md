# `docs/reports/` — Evaluation write-ups, audits, and reports

**Convention (automated):** every evaluation write-up, audit, or report gets its
own dedicated subdirectory under `docs/reports/`. Never drop a report into the
repo root, `docs/`, or anywhere else.

## Subdirectories

| Subdirectory | Contents | Example |
|---|---|---|
| `audits/` | Repository audits and synthesis reports | `AUDIT_SYNTHESIS_REPORT.md`, `PILOT_AUDIT_REPORT.md` |
| `pilots/` | Pilot-run evaluation write-ups (vision tradeoffs, run comparisons, etc.) | `pilot-vision-tradeoff.md` |
| `evaluations/` | Offline judge/quality evaluations over corpora (CUAD, MAUD, …) | — |
| `experiments/` | Synced experiment logs from the `llm-entity-extraction` prompt-experiment loop | `experiment_log.md` |

## Creating a new report

Use the scaffolder so every report lands in the right place with a consistent
header:

```bash
python scripts/new_report.py audits "MY AUDIT TITLE"
# -> creates docs/reports/audits/<date>-my-audit-title.md
python scripts/new_report.py pilots "Vision tradeoff" --date 2026-08-10
# -> creates docs/reports/pilots/2026-08-10-vision-tradeoff.md
python scripts/new_report.py evaluations "CUAD subclass sweep"
# -> creates docs/reports/evaluations/<date>-cuad-subclass-sweep.md
```

The scaffolder:

- creates the subdirectory if missing (also `evaluations/`),
- names the file `<date>-<kebab-case-slug>.md` (a dated, unique path),
- pre-fills a standard header (title, date, status, scope, method, findings,
  recommendations) so future write-ups are uniform and greppable.

## What goes where

- **Audits** (`audits/`) — codebase/repo audits, audit synthesis reports, issue
  follow-up audits. Each audit gets its own file (e.g. `2026-08-10-repo-audit.md`).
- **Pilots** (`pilots/`) — pilot-run write-ups: vision-vs-text tradeoffs, run
  comparisons, performance sweeps. `scripts/write_pilot_report.py` defaults here.
- **Evaluations** (`evaluations/`) — offline LLM-as-judge or deterministic
  evaluations over corpora (CUAD subclass sweeps, field-scoring calibration
  summaries, model comparisons).
- **Experiments** (`experiments/`) — **synced, NOT scaffolded**: mirrors of the
  experiment log from the upstream `llm-entity-extraction` repo
  (`docs/reports/experiments/experiment_log.md`, annotated with its upstream
  repo/commit). Never hand-edit it — re-sync from the source repo.

Rules: one report per file, dated filenames, no reports at repo root or inside
`docs/` outside this tree.
