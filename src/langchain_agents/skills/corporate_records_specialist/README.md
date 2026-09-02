<div align="center">

# 🏢 Corporate Records Specialist Skill

**Corporate records specialist skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts structured data from corporate records.

## Schema

| Field | Type |
|:---|:---|
| `entity_name` | `str` |
| `record_type` | `str` |
| `effective_date` | `str \| None` |
| `intent` | `str \| None` |
| `subject_matter` | `str \| None` |
| `keywords` | `list[str]` |
| `signatories` | `list[str]` |
| `jurisdiction` | `str \| None` |

## Related Files

- `../contracts_specialist/` — Contracts specialist
- `../correspondence_specialist/` — Correspondence specialist
