<div align="center">

# 📋 Compliance Specialist Skill

**Compliance specialist skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts structured data from compliance filings.

## Schema

| Field | Type |
|:---|:---|
| `filing_type` | `str` |
| `regulatory_body` | `str` |
| `filing_date` | `str \| None` |
| `entity_name` | `str` |
| `key_requirements` | `list[str]` |

## Related Files

- `../contracts_specialist/` — Contracts specialist
- `../corporate_records_specialist/` — Corporate records specialist
