# Judge Dimensions

Three independent audits — never mix them:

- Completeness (`mailroom-judge`): did the specialist capture every field the document states? 0 is a populated value, not empty.
- Classification (`mailroom-judge-classification`): is `doc_type` (and contract subtype) correct? `unknown` is valid when no live class fits.
- Correctness (`mailroom-judge-correctness`): are extracted values factually accurate? Identifiers must match as printed.

Judge only the registered schema for the assigned class. In-pipeline Lane B runs completeness only; the other two dimensions are offline (`run_quality_judges.py`).
