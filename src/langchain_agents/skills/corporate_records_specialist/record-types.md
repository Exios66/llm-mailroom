# Corporate Records — Extraction Skill

Extract the registered schema: `entity_name`, `record_type`, `effective_date`, `key_provisions`, `signatories`, `jurisdiction`, `filing_number`.

- `entity_name` is the legal name as written.
- `record_type` is the instrument (bylaws, resolution, articles, rights instrument) — not the parent filing that wraps it.
- A charter/bylaws BODY filed as an SEC exhibit is still `corporate_record`.
- Identifiers (`filing_number`) are transcribed exactly. Numeric zero is a stated value, not absence.
- Do not pull contract fields from an attached agreement.
