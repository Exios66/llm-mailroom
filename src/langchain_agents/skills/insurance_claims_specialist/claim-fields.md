# Insurance Claims — Extraction Skill

Extract: `claim_number`, `policy_number`, `insurer`, `insured_party`, `claim_type`, `date_of_loss`, `date_filed`, `claimed_amount`, `adjuster`, `damages_description`, `coverage_determination`, `denial_reasons`, `supporting_documents`.

- Identifiers are never paraphrased.
- `claimed_amount` of 0 is a stated amount. Do not compute or convert.
- `coverage_determination` only as written (`approved`, `denied`, `partial`, `pending`); never infer a denial.
- An insurance POLICY sold to the insured is a contract, not this schema.
- Denied claims must carry the written denial reasons; emptying `denial_reasons` on a denial is inconsistent.
