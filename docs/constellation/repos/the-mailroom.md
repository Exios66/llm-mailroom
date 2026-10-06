# The-Mailroom

**A pixel-art visual engine that draws every pipeline run as envelopes on a conveyor, driven only by Langfuse traces.**

| | |
| :--- | :--- |
| Repository | [Exios66/The-Mailroom](https://github.com/Exios66/The-Mailroom) |
| Monorepo path | `packages/The-Mailroom` (Python package `the-mailroom`) |
| Release | v0.5.0 |
| Site | [exios66.github.io/The-Mailroom](https://exios66.github.io/The-Mailroom/) |

## What it does

The-Mailroom renders the pipeline as a mailroom floor: sorter, specialist bays, judge gate, boss's desk, reporter and archive, grouped into three rooms, with a human-review siding. Click an envelope to inspect its full run.

| Surface | How to open it |
| :--- | :--- |
| Pixel-art console | `mailroom-web` → `http://127.0.0.1:8001/` |
| Hosted Observatory | `mailroom-hosted`, or `/live` on the same server |
| TUI | `mailroom-tui` |
| Terminal site | `/terminal/` on GitHub Pages, with a dataset viewer and a repo browser |

Console tabs: FLOOR, REVIEW (approve, reject, requeue, correct the class), INSPECTOR (span timeline, model, tokens, cost), SESSIONS, HISTORY with REPLAY, METRICS (including first-pass rate) and CONSOLE.

## How it connects to the pipeline

- **Langfuse is the only data source.** Every envelope, badge, verdict and metric comes from the pipeline's Langfuse project. Demo mode seeds synthetic runs into Langfuse rather than bypassing it.
- **It calls back into the pipeline API** for operator actions. Set `MAILROOM_PIPELINE_URL`, `MAILROOM_PIPELINE_TOKEN` and `MAILROOM_PIPELINE_API_PREFIX=/v1`. Queue a document posts to `/v1/upload`; REVIEW buttons call the pipeline's review-resolve endpoints. Producer keys never reach the browser.
- **Schema mirror duty.** When the pipeline changes spans, node order, agents, classes, thresholds or judge score names, update `mailroom_ui/pipeline_schema.py` and `mailroom_ui/trace_interpreter.py` in the same change window. `MAILROOM_TAXONOMY` can point at the pipeline's `taxonomy.yaml` directly.

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env      # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
mailroom-web              # pixel console on :8001
mailroom-tui              # typed-command console
```

Optional extras: `[pipeline]` (imports llm-mailroom for review resolve), `[operator]` (operator desk with auth, archive and observer), and a React desk under `ui/` that needs Node.

## Its documentation

- [README](https://github.com/Exios66/The-Mailroom/blob/main/README.md)
- [docs/architecture.md](https://github.com/Exios66/The-Mailroom/blob/main/docs/architecture.md)
- [docs/deployment.md](https://github.com/Exios66/The-Mailroom/blob/main/docs/deployment.md)
- [docs/operator-desk.md](https://github.com/Exios66/The-Mailroom/blob/main/docs/operator-desk.md)
- [docs/demos.md](https://github.com/Exios66/The-Mailroom/blob/main/docs/demos.md)
- [docs/releases.md](https://github.com/Exios66/The-Mailroom/blob/main/docs/releases.md)
- [Agent skills index](https://github.com/Exios66/The-Mailroom/blob/main/.cursor/skills/README.md)
- Pairing checklist on the pipeline side: [deploy/space/PAIRING.md](https://github.com/Exios66/llm-mailroom/blob/main/deploy/space/PAIRING.md)
