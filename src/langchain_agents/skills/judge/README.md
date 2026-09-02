<div align="center">

# ⚖️ Judge Skill

**Judge skill for the vendored LangChain agents.**

</div>

---

## Purpose

Verifies extraction completeness and correctness.

## Methods

| Method | Measures |
|:---|:---|
| `judge_completeness` | Did the specialist capture every field the document states? |
| `judge_classification` | Is the sorter's assigned class correct for the document? |
| `judge_extraction_correctness` | Are extracted values factually accurate (no fabrication)? |

## Output

Returns a score + label + reasoning for each dimension.

## Related Files

- `../arbiter/` — Arbiter skill
- `../contracts_specialist/` — Contracts specialist
