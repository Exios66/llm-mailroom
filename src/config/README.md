# `config/` — The single settings file

## What this folder is (plain English)

Just one file: `taxonomy.yaml`. It is the **control panel** for the whole pipeline. Almost everything you might want to tweak lives here, and nothing in the code hardcodes these values:

- **What kinds of documents** the pipeline recognizes (`doc_classes`) — contracts, corporate records, due diligence, correspondence, compliance filings, court opinions, each with its per-field `field_types:` scoring map.
- **Confidence thresholds** (`confidence:`) — how sure the LLM must be before the pipeline proceeds vs. retries vs. sends to a human.
- **Which LLM model each agent uses** (`agents:`) — e.g. `sorter` → OpenRouter `qwen/qwen3.7-flash`, or a local Ollama model.
- **Accepted file extensions** (`file_extensions:`).
- **Where files live on disk** (`pipeline.bins:`).
- **Deterministic field-scoring bands** (`field_scoring:` + `type_bands:`) — per-field-type judge-escalation cutoffs, calibrated by `scripts/calibrate_field_scoring.py` (issues #4/#5).
- **Vision-mode strategy** (`vision:`) — which agent models receive page images and the `max_pages` image budget.

If you change something here, **restart the watcher/API** — the config is cached in memory when the process starts and won't be picked up live.

## Technical reference

- Consumed by:
  - `pipeline/config.py` — `load_config()` (an `@lru_cache`), `get_agent_config(agent_name)`, `get_confidence_thresholds()`, `get_all_doc_types()`, `get_doc_class()`.
  - `pipeline/bins.py` — caches the config at module level to resolve bin paths (also `{base_dir}` variable → `MAILROOM_BASE_DIR` env, default `./data`).
  - `llm/providers.py` — per-agent `provider`/`model` resolution (see `llm/` README).
  - `observability/field_scoring.py` — `field_scoring:` config + `doc_classes[].field_types` drive the deterministic scorer's per-type bands.
  - `agents/sorter.py` — builds its classification prompt from `doc_classes` dynamically.
- `agents:` names must match each agent's `agent_name` class attribute in `agents/` (see `agents/` README).
- Editing `config/taxonomy.yaml` requires a process restart because of the `lru_cache` + module-level config cache.
- `python scripts/cutover.py` edits `agents:` in this file to switch agents between providers.
- Docker resources (compose file, README) live under `config/docker/`.
- Full reference with defaults: `docs/configuration.md`.
