# Contract Clause Categories — Contracts Specialist Skill Reference

The specialist extracts structured fields from contracts. These notes map the
41 CUAD clause categories to the extraction schema's fields so the model
knows what evidence to hunt for in each clause region of the document.

## Field → clause-category map

| Extraction field | CUAD categories to cite |
|---|---|
| `parties` | Parties, Document Name |
| `effective_date` | Agreement Date, Effective Date |
| `term_length` | Expiration Date, Renewal Term, Notice Period to Terminate Renewal |
| `termination_clauses` | Termination for Convenience, Post-Termination Services, Change of Control |
| `governing_law` | Governing Law |
| `key_obligations` | Most Favored Nation, Exclusivity, Minimum Commitment, Volume Restriction, Insurance, Audit Rights |
| `contract_value` | Revenue/Profit Sharing, Price Restrictions, Liquidated Damages, Cap on Liability, Uncapped Liability |
| `renewal_terms` | Renewal Term, Notice Period to Terminate Renewal |

## Evidence discipline

- Only cite text that is actually in the document — never infer obligations.
- A clause that is present but non-responsive (e.g. a warranty duration) is
  still evidence for `key_obligations` when it binds a party.
- When the document contains no value, term, or renewal language, the field
  must be null/empty — do not fabricate from recitals.
