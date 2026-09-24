# MAUD clauses — LegalBench question inventory

Emit answered questions only, formatted `<Question>: <Answer>` where
Answer is the Hub `valid_class`, not a paraphrase. Reuse
`langchain_agents.cuad_maud.MAUD_CLAUSE_QUESTIONS` (22 names). Flatten
Hub `maud_clause_labels` the same way contracts flatten CUAD spans
(`flatten_maud_clause_labels` → `as_clause_lines`).

## Questions

- Absence of Litigation Closing Condition
- Accuracy of Target R&W Closing Condition
- Agreement provides for matching rights in connection with COR
- Agreement provides for matching rights in connection with FTR
- Breach of Meeting Covenant
- Breach of No Shop
- Compliance with Covenant Closing Condition
- FTR Triggers
- Fiduciary exception to COR covenant
- Fiduciary exception:  Board determination (no-shop)
- General Antitrust Efforts Standard
- Intervening Event Definition
- Knowledge Definition
- Limitations on FTR Exercise
- MAE Definition
- Negative interim operating covenant
- No-Shop
- Ordinary course covenant
- Specific Performance
- Superior Offer Definition
- Tail Period & Acquisition Proposal Details
- Type of Consideration

## Consideration mapping

| Hub / text | Token |
|:---|:---|
| All Cash | `all_cash` |
| All Stock | `all_stock` |
| Mixed Cash/Stock | `mixed_cash_stock` |
| Mixed Cash/Stock: Election | `mixed_cash_stock_election` |
| other / unmatched | `other` |

Do not emit `cuad_family` or `cuad_clauses` on `merger_agreement`.
