---
name: langfuse
description: Configure and use Langfuse (SDK v4) as the tracing sink for llm-mailroom and The-Mailroom. Use when setting OBSERVABILITY_PROVIDER, document-pipeline traces, score configs, managed prompts, or MAILROOM_PIPELINE_URL for the visualizer.
---

# Langfuse (default tracing + visualizer)

**When:** Live/pilot traces, The-Mailroom floor, managed prompts, score
configs, LLM-as-a-judge evaluators.  
**Prefer over:** Phoenix (local fallback only) and Braintrust (opt-in hosted)
when The-Mailroom or family `document-pipeline` traces are required.

## Contract

| Knob | Value |
| --- | --- |
| SDK | langfuse 4.x (`create_trace_id`, `propagate_attributes`) |
| Cloud | `https://us.cloud.langfuse.com` project `llm-mailroom` |
| Root | `document-pipeline` (`chain`) |
| Nodes | verb-first `traced_node` names; types in `NODE_OBSERVATION_TYPES` |
| Tags | always `mailroom` + env (`pilot`/`live`) + `run-<n>` + `source-*` for corpora |
| Flush | after each graph node (live floor) and before process exit |

```bash
OBSERVABILITY_PROVIDER=langfuse   # or auto when LANGFUSE_SECRET_KEY is set
LANGFUSE_HOST=https://us.cloud.langfuse.com   # or http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Local compose (optional):

```bash
docker compose -f src/config/docker/docker-compose.yml up -d postgres clickhouse langfuse-server
PYTHONPATH=src python src/scripts/sync_prompts.py
```

## Family rules (do not break)

1. One document → one root `document-pipeline` chain; deterministic trace id from filename.
2. Child names stay verb-first (`classify-document`, `extract-fields`, `normalize-intake`, …).
3. Curated IO only (file metadata, not raw payloads).
4. Public GT keys: `expected_hf_class`, `expected_doc_class`, `expected_subclass`, `expected`.
5. Score names must exist in dojo `SCORE_CONFIGS` (35-char Langfuse aliases in `scores.py`).
6. Implementation: `observability/tracing.py` + `observability/langfuse_setup.py`.

## The-Mailroom

Visualizer is read-only Langfuse. REVIEW resolve still needs a **reachable
producer** — point it at this API (local or the published Space):

```bash
MAILROOM_PIPELINE_URL=http://127.0.0.1:8000
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
# MAILROOM_PIPELINE_API_PREFIX=/v1
```

Local: `docker compose -f deploy/docker-compose.producer.yml --env-file .env up -d --build`  
Hosted: `PYTHONPATH=src python src/scripts/publish_space.py --check` then `--repo <user>/mailroom-producer`.

Phoenix spans are **not** plottable there. Producer health lamp:
`GET /health` → `checks.watcher` / `inbox_pending` plus `producer` /
`review_resolve`.

## Depth

CLI, prompt migration, instrumentation, judge calibration:
`.opencode/skills/langfuse/` (github.com/langfuse/skills).

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Fallbacks: [apache-phoenix](../apache-phoenix/SKILL.md) · [braintrust](../braintrust/SKILL.md)
