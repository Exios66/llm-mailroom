<div align="center">

# 📄 Contracts Specialist Skill

**Contracts specialist skill for the vendored LangChain agents.**

</div>

---

## Purpose

Extracts structured data from CUAD commercial contracts. MAUD merger
agreements are handled by [`../merger_agreement_specialist/`](../merger_agreement_specialist/).

## Schema

| Field | Type |
|:---|:---|
| `document_name` | `str \| None` |
| `parties` | `list[str]` |
| `effective_date` | `str \| None` |
| `term_length` | `str \| None` |
| `governing_law` | `str \| None` |
| `contract_value` | `str \| None` |
| `cuad_clauses` | `list[str]` |
| `maud_clauses` | `list[str]` |

## Prompt Version

Production: `contracts_specialist_v33`

## Related Files

- `../corporate_records_specialist/` — Corporate records specialist
- `../correspondence_specialist/` — Correspondence specialist
