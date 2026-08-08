# Development Guide

## Project Structure

```
mailroom/
  agents/          # Specialist agents (Sorter, Contract, Corp, etc.)
  graph/           # LangGraph: state, nodes, routing
  llm/             # Provider-agnostic client
  schemas/         # Pydantic models
  pipeline/        # Watcher, bins, ops monitor
  storage/         # Postgres: catalog, audit log
  api/             # FastAPI
  observability/   # Langfuse callbacks
  config/          # taxonomy.yaml
  docker/          # docker-compose
  tests/           # pytest suite
  docs/            # In-repo documentation
  wiki/            # GitHub wiki pages
```

---

## Development Setup

```bash
pip install -e ".[dev]"
docker compose -f docker/docker-compose.yml up -d postgres
```

## Running Tests

```bash
pytest tests/ -v                          # All tests
pytest tests/test_agents/ -v              # Agent unit tests
pytest tests/test_routing.py -v           # Routing logic
pytest tests/test_audit_log.py -v         # Hash chain
pytest tests/test_pipeline_e2e.py -v      # E2E pipeline
pytest tests/ --cov=. --cov-report=html   # Coverage
```

## Test Structure

- **Unit tests**: 25 agent tests with mocked LLM calls
- **Routing tests**: 12 conditional edge tests
- **Audit tests**: 9 hash chain integrity tests
- **E2E tests**: 4 full pipeline tests with mocked LLM

## Adding a New Document Type

1. Add entry to `config/taxonomy.yaml` under `doc_classes`
2. Create Pydantic schema in `schemas/documents.py`
3. Register in `EXTRACTION_SCHEMAS` dict
4. Create agent class in `agents/` (extend `BaseAgent`)
5. Add dispatch entry in `graph/build_graph.py` (`extract_node` and `retry_extract_node`)
6. Add agent config under `agents` in `config/taxonomy.yaml`
7. Add test fixtures in `tests/fixtures/<new_type>/`
8. Add unit tests in `tests/test_agents/`

## Adding a New Provider

1. Add provider config to `llm/providers.py` in `_build_providers()`
2. Add default models to `DEFAULT_MODELS` dict
3. Add agent model mapping in `config/taxonomy.yaml`

## LangGraph Node Contract

Every node function signature:

```python
def node_name(state: DocumentState) -> dict[str, Any]:
    # state: current DocumentState TypedDict
    # return: dict of fields to update in state
    ...
```

Conditional edge functions:

```python
from typing import Literal
def routing_fn(state: dict) -> Literal["node_a", "node_b", "node_c"]:
    ...
```

## Key Design Rules

1. No provider-specific code in agents — use `BaseAgent.__init__` for LLM client
2. No hardcoded thresholds or doc types — everything reads from `config/taxonomy.yaml`
3. All filesystem operations go through `pipeline/bins.py` — never direct `os.rename`/`shutil.move`
4. Audit entries are created by `build_audit_entry()` in `schemas/audit.py` — never manual hash computation
5. Langfuse is optional — use `observability/langfuse_setup.py` which has noop fallback
