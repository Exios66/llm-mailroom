# `tests/` — Making sure it works

## What this folder is (plain English)

This is the automated safety net. It verifies the pipeline does what it's supposed to — without ever making a real (paid) AI call. All LLM responses are **mocked** (faked): the tests pretend an AI answered, so they're fast, free, and repeatable. They also run without Docker or any database server — SQLite files are created in a temp folder.

## How to run them

```bash
pip install -e ".[dev]"     # once, to get pytest

pytest tests/ -v                        # everything
pytest tests/test_agents/ -v            # agent unit tests
pytest tests/test_routing.py -v         # confidence/routing logic
pytest tests/test_audit_log.py -v       # hash-chain integrity
pytest tests/test_pipeline_e2e.py -v    # full pipeline run
pytest tests/ -v -k "sorter"            # just tests whose name matches
pytest tests/ --cov=. --cov-report=html # coverage report
```

## What's covered

- **`test_agents/`** — unit tests for the Sorter, all 6 specialists, and Boss. Asserts schema conformance and confidence-path behavior against mocked LLM output.
- **`test_routing.py`** — every conditional edge: high confidence → proceed, low → retry, still low → review, conflict → Boss, Boss → report/review, review → report/failed.
- **`test_audit_log.py`** — hash chaining, tamper detection, broken-link detection.
- **`test_pipeline_e2e.py`** — builds the whole 11-node graph and runs documents through it (happy path → archived, ambiguous → review, intake → manifest).

## Technical reference

- `conftest.py` sets `OPENROUTER_API_KEY` and `MAILROOM_BASE_DIR` (autouse fixture) so nothing touches the network or your real data dir.
  - `temp_base_dir` fixture — a fresh temp `MAILROOM_BASE_DIR` with all bins created.
  - `mock_openai_client` / `mock_low_confidence_client` — patch `llm.client.OpenAI` and `agents.base.BaseAgent.__init__` so agents get a fake client + `model = "test-model"`.
- **Writing a new agent test:** instantiate the agent, then inject `agent.client = <mock>` and `agent.model = "test-model"` exactly like the existing tests do. Never hit a real API.
- `asyncio_mode = "auto"` is set in `pyproject.toml`; graph nodes are sync. Test data is plain-text fixtures under `tests/fixtures/<doc_type>/` (e.g. `contract/sample_msa.txt`).
- The E2E graph uses the SQLite checkpointer, which falls back to in-memory if anything fails — tests pass either way.
- Deeper guide: `docs/testing.md` (mirrors `wiki/Development.md` testing sections).
