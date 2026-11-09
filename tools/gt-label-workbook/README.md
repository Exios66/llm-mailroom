# gt-label-workbook

Builds an editable Excel workbook of the ground truth the Dojo scores against, so missing
labels can be found and filled in.

```bash
PYTHONPATH=src .venv/bin/python tools/gt-label-workbook/build_gt_workbook.py --out gt_label_workbook.xlsx
```

- **Input:** `config=ground_truth` (train + test) of the pipeline corpus at `FULL_CORPUS_REVISION`
  (`src/pipeline/hf_corpora.py`). It is loaded read-only through `pipeline.hf_corpus_loader`, which needs
  network access to the Hub. `--revision` picks another revision. `--parquet-dir DIR` builds
  offline from `gt_train.parquet` and `gt_test.parquet`.
- **Output sheets:**
  - **README:** how to edit, and the status codes.
  - **Summary:** live coverage formulas.
  - **Date Audit:** every date label, one span per row, with the year, month and day it states. Labels
    flagged `MISSING LABEL`, `NO YEAR`, `NO DAY/MONTH` or `UNPARSEABLE` need a corrected value.
  - **One sheet per class:** a value column and a status column for every field the Dojo scores,
    including fields the dataset never labels (`UNLABELED`, highlighted yellow).
- **Columns follow the Dojo:**
  - `extraction_schema(doc_type).field_types`;
  - corporate-record subclass entity fields;
  - the CUAD category → field map (contract dates come from the CUAD `Agreement Date`, `Effective Date`
    and `Expiration Date` spans).

  Bumping the Dojo pin updates the workbook layout.
- **Dates:** stated components come from the Dojo's `_parse_date_parts` (llm-dojo-scoring#134) when the
  installed Dojo has it. Otherwise an audit-only copy of the same rule (`_fallback_date_parts`) is used.
  It never reads the clock and never scores anything. Remove it after the Dojo pin includes #134.
- **Read-only:** the workbook is a working copy. Edited labels reach the Hub only through the dataset
  owner's reviewed publish step. This tool never writes to Hugging Face.

Tests: `pytest src/tests/test_gt_label_workbook.py -q` (synthetic rows, no network).
