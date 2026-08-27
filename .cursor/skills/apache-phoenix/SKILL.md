---
name: apache-phoenix
description: Local cost-free Arize Phoenix OTEL fallback for llm-mailroom. Use when enabling PHOENIX_TRACING, OBSERVABILITY_PROVIDER=phoenix, or Arize UI on :6006 — not as the default The-Mailroom sink (prefer Langfuse).
---

# Apache Phoenix (local cost-free fallback)

**When:** User asks for Phoenix/Arize, OTEL UI on `:6006`, or
`OBSERVABILITY_PROVIDER=phoenix`. Also the `auto` chain's last tracing
backend when Langfuse/Braintrust keys are unset (`PHOENIX_TRACING` default
`enabled` so tracing never silently turns off).  
**Prefer Langfuse** whenever The-Mailroom or the family `document-pipeline`
contract is required — Phoenix spans are **not** plottable there.

## Enable

```bash
phoenix serve          # UI http://localhost:6006
```

```bash
OBSERVABILITY_PROVIDER=phoenix   # or auto without Langfuse/Braintrust keys
PHOENIX_TRACING=enabled
PHOENIX_ENDPOINT=http://localhost:6006/v1/traces
PHOENIX_PROJECT=mailroom
```

There is **no** Phoenix service in `src/config/docker/docker-compose.yml`
(unlike the sandbox). Run `phoenix serve` locally. Tests set
`OBSERVABILITY_PROVIDER=none` and should not require the UI.

## Boundaries

| Do | Don't |
| --- | --- |
| Use as the cost-free `auto` fallback | Replace Langfuse for visualizer demos |
| Set `PHOENIX_TRACING=disabled` only when you want `auto` to become `none` | Require GPU |
| Keep The-Mailroom pointed at Langfuse | Claim Phoenix feeds the pixel floor |

Setup lives in `src/observability/phoenix_setup.py` (OpenTelemetry-native,
SQLite, no subscription).

## Related

- Default sink: [langfuse](../langfuse/SKILL.md)
- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
