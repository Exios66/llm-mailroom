<div align="center">

# 🤝 Merger Agreement Specialist Skill

**Dedicated MAUD merger-agreement specialist for the mailroom extract class.**

</div>

---

## Purpose

Extracts structured data from Agreement and Plan of Merger documents
(including amended and restated forms). This is a distinct live class
from CUAD `contract` — predicting `contract` when ground truth is
`merger_agreement` is a miss.

## Schema (`MergerAgreementExtraction`)

| Field | Type |
|:---|:---|
| `document_name` | `str \| None` |
| `parties` | `list[str]` |
| `effective_date` | `str \| None` |
| `effective_time` | `str \| None` |
| `governing_law` | `str \| None` |
| `merger_consideration` | `str \| None` |
| `maud_clauses` | `list[str]` |
| `intent` | `str \| None` |
| `subject_matter` | `str \| None` |
| `keywords` | `list[str]` |

`cuad_family` and `cuad_clauses` are **not** primary on this class — omit them.

## Consideration tokens

`all_cash` · `all_stock` · `mixed_cash_stock` · `mixed_cash_stock_election` · `other`

## Related Files

- `../contracts_specialist/` — CUAD commercial-contract specialist
- `../../cuad_maud.py` — `MAUD_CLAUSE_QUESTIONS` / flatten helpers
