<div align="center">

# 🐍 LLM Dojo Scoring Package

**Core scoring engine for the LLM evaluation family.**

</div>

---

## Modules

| Module | Purpose |
|:---|:---|
| `scoring/` | Core scoring logic |
| `prompts/` | Prompt templates |
| `intake/` | Intake normalization |
| `entity_list/` | Entity list scoring |
| `regression/` | Regression diagnostics |

## Usage

```python
from llm_dojo_scoring import score_extraction
from llm_dojo_scoring.entity_list import score_entity_list
from llm_dojo_scoring.regression import compute_regression_diagnostics
```

## Related Files

- `tests/` — Test suites
- `examples/` — Usage examples
- `docs/` — Documentation
