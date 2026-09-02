<div align="center">

# 🏥 Insurance Claims Specialist Skill

**Insurance claims specialist skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts structured data from insurance claims.

## Schema

| Field | Type |
|:---|:---|
| `claim_number` | `str \| None` |
| `policy_number` | `str \| None` |
| `insurer` | `str` |
| `insured_party` | `str` |
| `claim_type` | `str` |
| `date_of_loss` | `str \| None` |
| `claimed_amount` | `float \| None` |
| `coverage_determination` | `str` |
| `denial_reasons` | `list[str]` |

## Related Files

- `../contracts_specialist/` — Contracts specialist
- `../correspondence_specialist/` — Correspondence specialist
