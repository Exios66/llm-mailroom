# gt-label-workbook

Builds an editable Excel workbook of the ground truth the Dojo scores against, so missing
labels can be found and filled in.

```bash
PYTHONPATH=src .venv/bin/python tools/gt-label-workbook/build_gt_workbook.py --revision v9.3 --out gt_label_workbook.xlsx
```

`--revision` defaults to `FULL_CORPUS_REVISION` (v9.1 at the time of writing); pass the release you want to label.

- **Input:** `config=ground_truth` (train + test) of the pipeline corpus at `FULL_CORPUS_REVISION`
  (`src/pipeline/hf_corpora.py`). It is loaded read-only through `pipeline.hf_corpus_loader`, which needs
  network access to the Hub. `--revision` picks another revision. `--parquet-dir DIR` builds
  offline from `gt_train.parquet` and `gt_test.parquet` (plus `default_train.parquet` and
  `default_test.parquet` for the text, see `--to-label`).
- **Output sheets:**
  - **README:** how to edit, and the status codes.
  - **Summary:** live coverage formulas.
  - **Date Audit:** every date label, one span per row, with the year, month and day it states. Labels
    flagged `MISSING LABEL`, `NO YEAR`, `NO DAY/MONTH` or `UNPARSEABLE` need a corrected value.
  - **One sheet per class:** a value column and a status column for every field the Dojo scores,
    including fields the dataset never labels (`UNLABELED`, highlighted yellow).
  - **`To Label – <Class>`** (insurance claims, corporate records, correspondence by default): one row
    per document with a field still to label, sorted by subclass then `document_id`. Columns:
    `document_id`, `split`, `subclass`, `filename`, `Open original` (only when the dataset gives a source
    link), `Open text`, `fields needing labels`, then a yellow value cell and a status cell for each field
    that needs a label on at least one row, then `still missing (live)` and `date flags`. A document is listed
    when a field is `UNLABELED` or `pending_annotation`, or a date label is flagged `NO YEAR`,
    `NO DAY/MONTH` or `UNPARSEABLE`. Dates must include the year.
  - **`Text – <Class>`**: the full document text from the `default` config, one block per document (a
    header row with a `back to To Label` link, then the text). Excel holds 32,767 characters per cell, so
    text is split over consecutive rows of at most 32,000 characters, at blank lines or line breaks where
    possible; the joined rows equal the original. Control characters XML cannot store are shown as their
    symbol (U+0001 becomes U+2401), one for one. If the texts would exceed `--max-workbook-mb` (default 150),
    they are written as `<out>_texts/<class>/<document_id>.txt` and `Open text` links to them with
    relative paths (keep the folder beside the workbook).
- **`--to-label CLASSES`:** comma-separated class keys, default `insurance_claim,corporate_record,correspondence`;
  `none` builds the workbook without To Label and Text sheets.
- **Links:** the dataset repo holds no original files (its listing is parquet and jsonl only), so there is no
  Hub file link. `Open original` uses the dataset's own `exhibit_url` for SEC EDGAR exhibits (corporate
  records); insurance and correspondence rows carry no source URL, so that column is left out of their
  sheets. A file the repo does hold would link to `huggingface.co/datasets/<id>/blob/<commit sha>/<path>`.
  sec.gov answers scripts with HTTP 403 (undeclared automated tool), so these links open in a browser but
  cannot be checked from a script here. The dataset viewer's `?q=` search is not used on these sheets because
  it could not be verified.
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
