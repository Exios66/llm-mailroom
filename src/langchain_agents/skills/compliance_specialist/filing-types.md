# Compliance Filings — Extraction Skill

Extract: `filing_type`, `regulatory_body`, `filing_date`, `due_date`, `entity_name`, `key_requirements`, `status`, `reference_number`.

- Name the filing type specifically (`10-K annual report`, not merely `SEC filing`).
- `reference_number` is an identifier (accession, control, file number); transcribe it exactly.
- An agreement filed as an SEC exhibit is extracted as a filing only when THIS document's form is the filing wrapper.
- Unstated dates are null. A stated 0 is a value.
