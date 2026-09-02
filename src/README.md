<div align="center">

# 🐍 Mailroom Source

**Source code for the llm-mailroom package.**

</div>

---

## Structure

| Path | Contents |
|:---|:---|
| [`agents/`](agents/) | LLM agent implementations |
| [`api/`](api/) | HTTP API endpoints |
| [`config/`](config/) | Configuration |
| [`graph/`](graph/) | Document graph (state machine) |
| [`langchain_agents/`](langchain_agents/) | Vendored LangChain agents |
| [`legalbench/`](legalbench/) | LegalBench integration |
| [`llm/`](llm/) | LLM client |
| [`observability/`](observability/) | Observability |
| [`pipeline/`](pipeline/) | Pipeline orchestration |
| [`schemas/`](schemas/) | Data schemas |
| [`scripts/`](scripts/) | Utility scripts |
| [`storage/`](storage/) | Storage backend |
| [`tests/`](tests/) | Test suites |

## Usage

```python
from agents.sorter import SorterAgent
from pipeline.document_graph import DocumentGraph
```

## Related Files

- `docs/` — Documentation
- `deploy/` — Deployment configuration
