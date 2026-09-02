<div align="center">

# ✉️ Correspondence Specialist Skill

**Correspondence specialist skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts structured data from correspondence (letters, emails, memos).

## Schema

| Field | Type |
|:---|:---|
| `sender` | `str` |
| `recipient` | `str` |
| `additional_recipients` | `list[str]` |
| `communication_type` | `str` |
| `communication_date` | `str \| None` |
| `intent` | `str \| None` |
| `subject_matter` | `str \| None` |
| `keywords` | `list[str]` |
| `demand_amount` | `float \| None` |
| `action_items` | `list[str]` |
| `urgency` | `str` |

## Related Files

- `../contracts_specialist/` — Contracts specialist
- `../corporate_records_specialist/` — Corporate records specialist
