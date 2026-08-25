# Testing Guide

## Test Structure

```
tests/
├── conftest.py                  # Shared fixtures and mocks
├── test_agents/
│   ├── __init__.py
│   ├── test_sorter.py           # Sorter agent unit tests
│   └── test_specialists.py      # All specialist + Boss unit tests
├── test_routing.py              # Confidence-based routing logic
├── test_audit_log.py            # Hash-chain integrity tests
├── test_pipeline_e2e.py         # End-to-end pipeline tests
└── fixtures/
    ├── contract/                # 3 sample contracts (MSA, NDA, ambiguous)
    ├── corporate_record/        # 2 sample corp records (bylaws, resolution)
    ├── due_diligence/           # retired class (fixtures kept on disk)
    ├── court_opinion/           # retired class (fixtures kept on disk)
    ├── correspondence/          # 2 sample correspondences (demand letter, memo)
    ├── compliance_filing/       # 2 sample filings (10-K, state filing)
    └── insurance_claim/         # FNOL / claim documentation
```

---

## Running Tests

```bash
# All tests
pytest -v

# By test file
pytesttest_agents/test_sorter.py -v
pytesttest_agents/test_specialists.py -v
pytesttest_routing.py -v
pytesttest_audit_log.py -v
pytesttest_pipeline_e2e.py -v

# By test name pattern
pytest -v -k "sorter"

# With coverage
pytest --cov=. --cov-report=html --cov-report=term

# With verbose output
pytest -v -s
```

---

## Test Categories

### Agent Unit Tests (`test_agents/`)

**37 tests** covering:
- Sorter: classification across all doc types, low-confidence cases, JSON parse errors, output validation
- Contracts Specialist: extraction accuracy, confidence scoring
- Corporate Records Specialist: entity/record extraction
- Due Diligence Specialist: risk flag detection
- Correspondence Specialist: action item extraction
- Compliance Specialist: filing type identification
- Boss Agent: adjudication decisions, system metrics analysis

All LLM calls are **mocked** — tests assert schema conformance and confidence-path branching without real API calls.

### Routing Tests (`test_routing.py`)

**12 tests** covering every conditional edge:
- High confidence → proceed
- Low confidence → retry → retry again → human review
- Conflict detection → Boss escalation
- Boss decision → compile_report or human review
- Human review → approved or failed

### Audit Log Tests (`test_audit_log.py`)

**9 tests** covering:
- Hash computation and chaining
- Chain verification (valid chains)
- Tamper detection (modified hash)
- Broken link detection (wrong prev_hash)
- Empty chain handling
- Deterministic hashing

### E2E Pipeline Tests (`test_pipeline_e2e.py`)

**4 tests** covering full pipeline runs:
- Happy path: contract document → archived
- Low confidence path: ambiguous document → review
- Ingest node: manifest creation and file reading
- Full pipeline with mocked LLM: correspondence → archived

These tests spin up a complete LangGraph graph with all 13 nodes and mock the LLM layer, verifying:
- State flows through all nodes correctly
- Files move between bins
- Archive paths are correct
- Manifests are created

---

## Test Fixtures

### Pilot sample set

For live end-to-end pilots (not the unit suite), see `docs/examples/samples/`: 30 legal
PDFs (real CC-BY-4.0 CUAD/Atticus contracts + LegalBench MAUD merger agreements +
public-domain Pile of Law court opinions + repo-written synthetic text) with a
ground-truth `manifest.csv`, built by `scripts/prepare_samples.py` (and
`scripts/fetch_external_samples.py` for the external corpus) and evaluated by
`scripts/run_pilot.py` (`--mock` for a deterministic run over the live 22-sample
set, `--real` for actual LLM accuracy on the 21 real committed documents, 
`--baseline` to diff two runs, `--source <corpus>` to run one dataset). Real
runs are restricted to the actual committed legal documents (CUAD/Atticus PDFs,
LegalBench, Pile of Law); the repo-written synthetic `.txt` samples are mock-only
and are refused by `--real`. See `docs/examples/samples/README.md`.

### Shared Fixtures (`conftest.py`)

| Fixture | Description |
|---|---|
| `temp_base_dir` | Creates a temporary `MAILROOM_BASE_DIR` with all bin directories |
| `mock_openai_client` | Mocks `llm.client.OpenAI` with a high-confidence contract response |
| `mock_low_confidence_client` | Mocks with a low-confidence response |
| `sample_*_text` | Reads fixture files for each doc type |
| `all_fixture_files` | Dictionary of all fixture file contents |

### Document Fixtures (`src/tests/fixtures/`)

| Fixture | Type | Purpose |
|---|---|---|
| `sample_msa.txt` | Contract | Full Master Services Agreement — happy path |
| `sample_nda.txt` | Contract | NDA — simpler contract variant |
| `ambiguous_doc.txt` | Contract | Deliberately vague — tests low-confidence path |
| `sample_bylaws.txt` | Corporate Record | Full corporate bylaws |
| `sample_resolution.txt` | Corporate Record | Board resolution |
| `sample_dd_report.txt` | Due Diligence | Comprehensive DD report with risk flags |
| `sample_checklist.txt` | Due Diligence | Simple DD checklist — tests sparse data |
| `sample_demand_letter.txt` | Correspondence | Formal demand letter — action items |
| `ambiguous_memo.txt` | Correspondence | Interoffice memo mixing multiple doc types |
| `sample_10k.txt` | Compliance Filing | SEC 10-K filing |
| `sample_state_filing.txt` | Compliance Filing | State annual report |
| `sample_opinion.txt` | Court Opinion | Appellate opinion — exercises suppression + weight-of-evidence issues |

---

## Writing New Tests

### Agent Unit Test Pattern

```python
class TestNewAgent:
    def test_extract(self, sample_text, mock_openai_client):
        # Set the mock response
        mock_openai_client.chat.completions.create.return_value \
            .choices[0].message.content = '{"field": "value", "confidence": 0.95}'

        # Import and instantiate
        from agents.new_specialist import NewSpecialist
        agent = NewSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"

        # Call and assert
        result = agent.extract(sample_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert "value" in result.get("field", "")
```

### Routing Test Pattern

```python
from graph.routing import after_classify

def test_routing_scenario():
    state = {
        "classification_confidence": 0.50,
        "classification_attempts": 1,
        "doc_type": "contract",
    }
    assert after_classify(state) == "retry_classify"
```

### E2E Test Pattern

```python
def test_full_pipeline(self, temp_base_dir, mock_openai_client):
    from graph.build_graph import build_graph

    # Create test file
    inbox = temp_base_dir / "pipeline" / "inbox"
    test_file = inbox / "test.txt"
    test_file.write_text("Document content...")

    # Build graph and run
    graph = build_graph()
    config = {"configurable": {"thread_id": "test-1"}}
    result = graph.invoke(initial_state, config)

    # Assert final state
    assert result["stage"] == "archived"
```

---

## Test Configuration

In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]
```

Tests auto-discover asyncio fixtures. No `@pytest.mark.asyncio` decorator needed for sync tests — the graph now uses sync nodes.
