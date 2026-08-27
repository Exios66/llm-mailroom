---
name: braintrust
description: Opt-in Braintrust observability and the agent-auto-improvement loop for llm-mailroom. Use when BRAINTRUST_API_KEY is set, OBSERVABILITY_PROVIDER=braintrust|auto selects it, or the user wants production-trace evals — never as the default live or The-Mailroom sink.
---

# Braintrust (opt-in hosted)

**When:** Explicit Braintrust request, or `auto` with `BRAINTRUST_API_KEY` and
no Langfuse secret.  
**Prefer Langfuse** for live traces + The-Mailroom. Braintrust is a hosted
escape hatch and the **auto-improvement** loop, not the default producer sink.

## Enable

```bash
OBSERVABILITY_PROVIDER=braintrust   # or auto
BRAINTRUST_API_KEY=...
BRAINTRUST_PROJECT=mailroom
```

`auto` order: Langfuse → **Braintrust** → Phoenix → none.

Pytest forces `OBSERVABILITY_PROVIDER=none` and pops `BRAINTRUST_API_KEY`.

## What this repo uses Braintrust for

- Optional trace backend (`observability/braintrust_setup.py`)
- Eval discipline aligned with `llm-entity-extraction` (datasets, Eval
  loops, experiment log)

The-Mailroom does **not** read Braintrust.

## Auto-improvement loop (depth)

Production traces → failure taxonomy → remote dataset → scorers → offline
eval file → iterate → push online scorers. Follow the vendored skill
`.opencode/skills/braintrust/` (github.com/braintrustdata/braintrust-skills).
Scoring formulas themselves stay in [dojo-scoring](../dojo-scoring/SKILL.md).

## Related

- Default sink: [langfuse](../langfuse/SKILL.md)
- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
