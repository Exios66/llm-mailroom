<div align="center">

# 📋 Sorter Skill

**Sorter skill for the vendored LangChain agents.**

</div>

---

## Purpose

Classifies documents into one of the configured document classes.

## Output

| Field | Type |
|:---|:---|
| `doc_type` | `str` |
| `contract_subtype` | `str \| None` |
| `confidence` | `float` |
| `reasoning` | `str` |

## Prompt Version

Production: `sorter_v14`

## Related Files

- `../sorter_reviewer/` — Independent classification review
- `../contracts_specialist/` — Contract extraction
