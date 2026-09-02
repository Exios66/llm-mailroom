<div align="center">

# 🔍 Sorter Reviewer Skill

**Sorter reviewer skill for the vendored LangChain agents.**

</div>

---

## Purpose

Provides an independent second opinion on document classification.

## Key Feature

The reviewer never sees the sorter's label — independence is the point.

## Output

| Field | Type |
|:---|:---|
| `doc_type` | `str` |
| `contract_subtype` | `str \| None` |
| `doc_subclass` | `str \| None` |
| `confidence` | `float` |

## Related Files

- `../sorter/` — Primary classification
- `../judge/` — Completeness verification
