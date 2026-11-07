# CLAUDE.md — llm-mailroom

LangGraph pipeline that processes legal documents through specialist LLM agents
(intake → classify → extract → judge/arbiter → report → archive), with filesystem bins,
a SQLite catalog and hash-chained audit log, a FastAPI producer on :8000, and
Langfuse / Braintrust / Phoenix tracing. Python 3.11+. This repo **owns pipeline
behavior**: the 13-node graph, agents, prompts, storage and API.

`AGENTS.md` is the long-form manual (~55 KB) and is **not** auto-loaded. Read the matching
section on demand ("Architecture", "Config gotchas", "Testing quirks", "Releases",
"Source ownership") instead of guessing.

**Binding on every task** (`.claude/rules/`; Claude Code loads these automatically, other
tools must read them):

- `orchestration.md` — Opus orchestrates; Sonnet and Haiku subagents do the work; 1–3
  adversarial review rounds; orchestrator re-verifies.
- `evidence-contract.md` — what counts as proof before anything is called done.
- `pr-flow.md` — local gate → fresh-context pre-PR review → draft PR → bot review.

## Constellation: who depends on what

| Repo | Relationship to this repo |
|:---|:---|
| `Exios66/llm-dojo-scoring` | **Upstream.** Owns every metric and scoring rule. Pinned in `pyproject.toml` as a 40-hex git SHA; `vendor/llm-dojo-scoring/` is a fallback snapshot of the same commit served by `src/dojo_source/` only when the install fails. Scoring bugs are fixed there first. |
| `Exios66/local-mailroom-sandbox` | **Downstream.** Vendors a snapshot of this repo and the Dojo under its `vendor/` to run the pipeline offline (Ollama, vLLM, Modal). Changes reach it only through its own reviewed vendor refresh. |
| `Exios66/The-Mailroom` | **Downstream.** Visualizer driven by our Langfuse traces; calls our `/v1/upload` and review-resolve API. Changing spans, node order, agents, classes, thresholds or judge score names needs a matching change in its `mailroom_ui/pipeline_schema.py` and `trace_interpreter.py`. |
| `LLM-Mailroom-Services/Digital-Mailroom` | **Integration hub.** Imports our merged `main` into `packages/` and owns the cross-repo board (`governance/TASKS.md`). Never develop here from the hub, and never copy hub trees back. |
| `LLM-Mailroom-Services/mailroom-ml` | Optional ModernBERT intake fast path (`agents/bert_intake.py`, off by default, fail-open). |
| `Lucius-Morningstar/mailroom-dataset` (HF) | The corpus, pinned by revision; loaded through `pipeline/hf_corpus_loader.py`. |
| `LLM-Mailroom-Services/mailroom-issues` | Where cross-repo bugs and epics are filed. |

Cross-repo constellation docs live in `docs/constellation/` (GitBook root is `docs/`).

## File map

| Path | What lives there |
|:---|:---|
| `src/graph/` | `build_graph.py` (13 nodes, `run_pipeline`, tracing wrappers), `routing.py` (conditional edges) |
| `src/agents/` | LLM agents (`BaseAgent` subclasses: sorter, specialists, judge, boss, relations, triage) and procedural ones (reporter, archivist, pdf_transcriber, image_extractor, intake clerk) |
| `src/llm/` | `client.py:get_llm`, `providers.py`, `retry.py`, `prompts.py` (Langfuse-managed prompts + fallbacks), `vision.py` |
| `src/pipeline/` | watcher, bins, guards, Gmail intake, relations, ops monitor, pager, env/logging/config |
| `src/schemas/` | Pydantic document and manifest models, `EXTRACTION_SCHEMAS` |
| `src/storage/` | SQLite/Postgres catalog, audit log, relations tables |
| `src/observability/` | tracing backends, scores, field-scoring adapters to the Dojo |
| `src/api/` | FastAPI app (`main.py`), embeds the watcher |
| `src/config/taxonomy.yaml` | **Single source of truth**: classes, thresholds, agent → model, token caps |
| `src/scripts/` | operator and maintenance CLIs (pilots, gates, sync, release, bumps) |
| `src/tests/` | pytest suite; `conftest.py` mocks every LLM client |
| `src/legalbench/` | LegalBench evaluation submodule |
| `deploy/` | compose files, LiteLLM gateway, Modal vLLM, HF Space payload |
| `contract/` | generated display contract (`export_contract.py --write`) |
| `docs/` | GitBook site and reports (`docs/reports/<kind>/`); `docs/changelog/` is generated |
| `landing/`, `notebooks/` | landing page (JS tested by `test_landing.py`); notebooks 00–14 |
| `vendor/llm-dojo-scoring/` | fallback Dojo snapshot. **Never patch it.** |

## Setup and core commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # use PIP, never uv
.venv/bin/python -m pytest src/tests -q                       # full suite, no network (10+ min)
PYTHONPATH=src .venv/bin/python src/scripts/affected_tests.py --run -- -q   # only tests the diff can affect
.venv/bin/python -m pytest src/tests -k sorter -q             # one area
PYTHONPATH=src .venv/bin/python src/scripts/run_pilot.py --mock   # whole pipeline, fake LLM
PYTHONPATH=src .venv/bin/python -m api.main                   # API on :8000 (needs .env)
```

- **Do not use `uv sync` / `uv pip install -e .`.** `[tool.uv.sources]` points
  `llm-dojo-scoring` at a hub workspace that does not exist here; uv fails with
  "not a workspace member". `uv` is only used by one test as a build tool.
- `pyproject.toml` sets `pythonpath = ["src", "."]`, so bare `pytest` finds modules.
  Scripts and `python -m pipeline.*` need `PYTHONPATH=src`.
- Every other script and its flags are listed in `AGENTS.md` → "Commands".

## Verification gates

Run the ones that match the change, on the final state, and keep the output for the PR
(`.claude/rules/evidence-contract.md`). CI (`.circleci/config.yml`, job `test`) runs the
full suite and the experiment gate.

| Change touches | Run |
|:---|:---|
| Any Python | `.venv/bin/python -m compileall -q src`, then `affected_tests.py --run`, then the full suite before the PR is ready |
| Graph, agents, routing, prompts | + `run_pilot.py --mock` and `run_experiment.py --dataset mailroom-fixtures --mock --baseline docs/superpowers/baselines/experiment-baseline.json` |
| Nodes, spans, agents, classes, score configs, thresholds | + `PYTHONPATH=src .venv/bin/python src/scripts/export_contract.py --check` (`--write` to regenerate) |
| `CHANGELOG.md` | + `PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py --check` |
| Docs, README, prose with paths/versions | + `pytest src/tests/test_docs_truth.py -q` |
| `landing/` | + `pytest src/tests/test_landing.py -q` and screenshots |
| Dojo pin | the `/dojo-bump` skill (verifies against the real pinned revision) |

**No linter or type checker is configured — do not add one.** A PostToolUse hook only
syntax-checks edited Python files. Say "not configured" in the PR rather than claiming
lint or type cleanliness.

## Hard rules

- **No real LLM call ever runs in tests** (`src/tests/conftest.py` patches the client).
  **Never** run anything with `--real`, `sync_prompts`, `sync_dataset`, `sync_dashboards`,
  `sync_evaluators` or `sync_hf_ground_truth --push` unless the user asks: they spend money
  or write to hosted Langfuse / Hugging Face. Prefer `--mock` and `--check`.
- LLM access **only** via `get_llm(agent_name)` (`llm/client.py`). `agent_name` must be a key
  under `agents:` in `src/config/taxonomy.yaml`. No agent names a provider or model; all chat
  completions go through `llm/retry.py:retry_chat_completion`.
- `src/config/taxonomy.yaml` is the single source of truth (classes, thresholds, model
  mapping). Nothing is hardcoded in code.
- Files move **only** through `pipeline/bins.py` helpers — never `os.rename` or
  `shutil.move` in node/agent code. Use `review_file_path(manifest)` for review bins.
- Agent prompts are Langfuse-managed with an identical in-code fallback. A new or changed
  prompt must be registered in `llm/prompts.py:prompt_templates()`. Leave the `json_object`
  boilerplate in `agents/base.py:_call_structured` alone (Qwen rejects requests without the
  literal token `json`).
- Never log a Langfuse trace without tags (`AGENTS.md` → "classify and tag every logged run").
- Reporter / archivist / pdf_transcriber / image_extractor are **procedural**, not LLM agents.
- Documents are never truncated: long inputs go through sliding windows.

## Gotchas

- Config is `lru_cache`d (`pipeline/config.py`, `pipeline/bins.py`): taxonomy edits need a
  watcher/API restart.
- **New doc class = ~5 coordinated edits** (taxonomy `doc_classes` + `agents:`, schema +
  `EXTRACTION_SCHEMAS`, a `BaseAgent` subclass, `graph/build_graph.py:_specialist_extractor_map`,
  a prompt in `prompt_templates()`, tests, `export_contract.py --write`). Read `AGENTS.md` →
  "Config gotchas" first.
- Tests use `asyncio_mode = "auto"`, but graph nodes are sync. Node contract:
  `node(state: DocumentState) -> dict[str, Any]` (partial update).
- New agent tests inject `agent.client = <mock>` and `agent.model = "test-model"`.
- `test_wheel_runtime_catalog` runs `uv build --offline`, so uv's cache must already hold
  setuptools and wheel (the test skips if `uv` is missing). With an empty cache it can be
  the only failure — say so instead of calling the suite red.
- Langfuse trace ids are deterministic per filename and tags are immutable: a re-run keeps
  the first run's tags and environment.
- The suite is slow; use `affected_tests.py` while iterating, the full suite before "ready".

## Do not hand-edit (a hook blocks these)

- `.env` (secrets; only `.env.example` is tracked) and `data/` (runtime bins).
- `docs/changelog/` — edit `CHANGELOG.md`, then
  `PYTHONPATH=src .venv/bin/python src/scripts/sync_gitbook_changelog.py` (`--check` verifies).
- `docs/superpowers/baselines/experiment-baseline.json` — re-approve only with
  `run_experiment.py ... --write-baseline`, and only when the user asks.
- Versions: use `python3 src/scripts/release.py cut X.Y.Z` (dry run unless `--apply`).

## Subagents and skills

- Project subagents (`.claude/agents/`): `implementer` (Sonnet), `runner` (Haiku),
  `adversarial-reviewer` (Sonnet), `pre-pr-reviewer` (Sonnet), `trace-log-analyst`
  (Sonnet; read-only Langfuse trace review, canonical prompt in `.opencode/agents/`).
  How and when to use them: `.claude/rules/orchestration.md`.
- `/dojo-bump` (`.claude/skills/dojo-bump/`) — check or bump the Dojo pin and verify
  against the real pinned revision. User-invoked only.
- Domain skills for providers and tracing: `.cursor/skills/` (start with
  `mailroom-tool-router`) and `.opencode/skills/` (langfuse, langgraph, openrouter, …).

## Cross-repo rules

- Fix scoring bugs in **llm-dojo-scoring first**; bump the pin here with
  `src/scripts/bump_dojo_scoring.py` (`--check` is read-only). Never delete the pin.
- Never copy hub or sandbox `vendor/` trees back into this repo.
- If a change alters the producer API, trace shape or taxonomy, say in the PR which
  downstream repo needs a follow-up (The-Mailroom schema mirror, sandbox vendor refresh).
