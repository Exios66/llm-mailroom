# `agents/` — The LLM "people" who do the work

## What this folder is (plain English)

This is the cast of characters. Each file is one **agent**: a specialist LLM "worker" with its own job and its own personality (a system prompt). When a document flows through the pipeline, the graph calls these agents one at a time:

1. **Sorter** decides what kind of document it is (contract, corporate record, …).
2. A **Specialist** (contracts, corporate records, correspondence, compliance, or insurance claims) reads the document and extracts structured facts. Unclassifiable / retired types (`unknown`) skip extraction and go to human review.
3. **Reporter** writes a clean summary of everything that was extracted.
4. **Boss** steps in when there's a conflict or repeated low confidence and makes the final call.

Three of these files are **not** LLM agents — they're plain code helpers:

- `archivist.py` — moves the finished file into `data/archive/` and writes the audit entry.
- `intake.py` — deterministic whitespace / hyphen / NBSP normalize; emits span `normalize-intake` under ingest.
- `image_extractor.py` — turns an image into text (uses a vision LLM if available).
- `pdf_transcriber.py` — turns a PDF into text/markdown.

Every agent gets its LLM model from `config/taxonomy.yaml` (see `config/` README). **You never need to touch a provider name or model name in this folder** — that's all config.

## Technical reference

- `base.py` — `BaseAgent` (ABC). Subclasses set `agent_name`, which MUST match a key under `agents:` in `config/taxonomy.yaml`. `__init__` calls `get_llm(self.agent_name)` to get `(client, model)`.
  - `_call_llm(...)` — raw chat completion.
  - `_call_structured(...)` — LLM call using OpenAI JSON-schema mode; returns parsed `dict` (or `{"_parse_error": True, "_raw": ...}` on bad JSON).
  - `build_structured_schema(properties, required, ...)` — helper for JSON-schema payloads.
- Specialist agents (`*_specialist.py`) expose `.extract(doc_text) -> dict` that includes a `confidence` field; the graph pops `confidence` and uses it for routing.
- `reporter.py` — a plain function `compile_matter_record(manifest_data, report_llm, report_model)`, NOT a `BaseAgent`. Called from `graph/build_graph.py:compile_report_node`, which fetches its own LLM via `get_llm("reporter")`.
- `boss.py` — `BossAgent.adjudicate(manifest_data)` (in-graph escalation) and `BossAgent.analyze_system_metrics(metrics)` (used by `pipeline/ops_monitor.py`). Shared `BOSS_SYSTEM_PROMPT`.
- `image_extractor.py` and `pdf_transcriber.py` both set `agent_name = "sorter"` (they reuse the sorter's model config). They're invoked from `graph/build_graph.py` `_read_file_text()` based on file extension.
- `docs/agents.md` documents the full roster, schemas, and the "add a new agent" checklist.
