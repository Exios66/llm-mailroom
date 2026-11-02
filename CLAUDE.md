# CLAUDE.md — llm-mailroom

LangGraph pipeline that processes legal documents through specialist LLM agents
(classify → extract → judge/arbiter → report → archive) with filesystem bins, a
SQLite catalog/audit log and Langfuse/Phoenix/Braintrust tracing. Python 3.11+.
This repo **owns pipeline behavior**; scoring lives in `llm-dojo-scoring`, the
offline/Modal harness in `local-mailroom-sandbox`.

`AGENTS.md` is the long-form reference (~55 KB) and is **not** auto-loaded. Read
the relevant section on demand instead of guessing — especially "Architecture",
"Config gotchas", "Testing quirks" and "Source ownership". The rules below are the
ones that cause repeated errors.

## Setup and tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # use PIP, not uv
.venv/bin/python -m pytest src/tests -q                       # whole suite, no network
.venv/bin/python -m pytest src/tests -k sorter -q             # one area
PYTHONPATH=src python src/scripts/run_pilot.py --mock         # pipeline machinery, fake LLM
```

- **Do not use `uv sync` / `uv pip install -e .`.** `[tool.uv.sources]` redirects
  `llm-dojo-scoring` to a Digital-Mailroom *workspace* that does not exist in this
  standalone checkout, so uv fails with "not a workspace member". Plain `pip` works.
- `pyproject.toml` already sets `pythonpath = ["src", "."]`, so bare `pytest` finds
  modules. Scripts and `python -m pipeline.*` need `PYTHONPATH=src`.
- **No real LLM call ever runs in tests** (`src/tests/conftest.py` patches the
  client). Never run anything with `--real`, `sync_prompts`, `sync_dataset`,
  `sync_dashboards`, `sync_evaluators` or `sync_hf_ground_truth --push` unless the user
  asks: they spend money or write to hosted Langfuse/Hugging Face. Prefer `--mock`
  and `--check`.
- **No linter or type-checker is configured — do not add one.** A hook only does a
  syntax check on edited Python files.
- `tests` use `asyncio_mode = "auto"`; graph nodes are sync.

## Hard rules

- LLM access **only** via `get_llm(agent_name)` (`llm/client.py`). `agent_name` must
  be a key under `agents:` in `config/taxonomy.yaml`. No agent names a provider or
  model; all chat completions go through `llm/retry.py:retry_chat_completion`.
- `config/taxonomy.yaml` is the single source of truth (classes, thresholds, model
  mapping). Config is `lru_cache`d, so edits need a watcher/API restart.
- Files move **only** through `pipeline/bins.py` helpers — never `os.rename` or
  `shutil.move` in node/agent code. Use `review_file_path(manifest)` for review bins.
- Agent prompts are Langfuse-managed with an identical in-code fallback. A new or
  changed prompt must be registered in `llm/prompts.py:prompt_templates()`. Leave the
  `json_object` boilerplate in `agents/base.py:_call_structured` alone (Qwen rejects
  requests without the literal token `json`).
- New doc class = ~5 coordinated edits (taxonomy `doc_classes` + `agents:`, schema +
  `EXTRACTION_SCHEMAS`, a `BaseAgent` subclass, graph dispatch, tests). See
  AGENTS.md "Config gotchas" before starting.
- Never log a Langfuse trace without tags (see AGENTS.md "classify and tag every
  logged run").
- Reporter / archivist / pdf_transcriber / image_extractor are **procedural**, not
  LLM agents.

## Do not hand-edit (a hook blocks these)

- `.env` (secrets; only `.env.example` is tracked) and `data/` (runtime bins).
- `docs/changelog/` — regenerated: `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`
  (edit `CHANGELOG.md`, then regenerate; `--check` verifies).
- `docs/superpowers/baselines/experiment-baseline.json` — re-approve only with
  `run_experiment.py ... --write-baseline`, and only when the user asks.

## Cross-repo

- Fix scoring bugs in **llm-dojo-scoring first**; this repo consumes it as a git pin
  in `pyproject.toml` (a 40-hex SHA while a fix is unreleased). Bump via
  `src/scripts/bump_dojo_scoring.py` (`--check` is read-only) — never delete the pin.
- Never copy hub or sandbox `vendor/` trees back into this repo.
- Changes here reach the sandbox only through a reviewed vendor refresh there.
