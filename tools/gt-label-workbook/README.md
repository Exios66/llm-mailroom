# gt-label-workbook

Builds an editable Excel workbook of the ground truth the Dojo scores against, so missing
labels can be found and filled in.

```bash
PYTHONPATH=src .venv/bin/python tools/gt-label-workbook/build_gt_workbook.py --revision v9.3 --out ~/gt_label_workbook.xlsx
```

The workbook holds the full document text, so it is a private, offline record: write it
outside the repository. `*.xlsx` and the `<name>_texts/` fallback folder are git-ignored.

`--revision` (a tag, branch or commit; default `FULL_CORPUS_REVISION`, v9.1 at the time of writing) is resolved to a
commit sha through the Hub API (`/api/datasets/<id>/revision/<rev>`, read-only). A revision that does not resolve exits
non-zero before anything is downloaded. The parquet files are then downloaded from
`resolve/<sha>/parquet/<config>/<split>/`, so the workbook is built from exactly that commit (not from the viewer's
`refs/convert/parquet` branch, which can lag). The sha is printed in the run's JSON and written on the README sheet and in
the workbook properties.

- **Input:** `config=ground_truth` (train + test) and, for `--to-label`, `config=default` (the document text), as
  above; network read access to the Hub is needed. `--parquet-dir DIR` builds offline from `gt_train.parquet` and
  `gt_test.parquet` (plus `default_train.parquet` and `default_test.parquet` for the text); the commit is then not
  resolved and the README says so.
- **Output sheets:**
  - **README:** how to edit, and the status codes.
  - **Summary:** live coverage formulas.
  - **Date Audit:** every date label, one span per row, with the year, month and day it states. Labels
    flagged `MISSING LABEL`, `NO YEAR`, `NO DAY/MONTH` or `UNPARSEABLE` need a corrected value.
  - **One sheet per class:** a value column and a status column for every field the Dojo scores that the
    dataset labels for that class (a value or a `gt_presence` key on at least one row); a field with no label
    yet is `UNLABELED`, highlighted yellow. A pipeline output field the dataset never carries for a class
    (for example insurance `claim_checklist`) is not a labelling target and gets no column; the README sheet
    and the build summary (`dropped_fields`) list them. Corporate records keep the Dojo's per-subclass entity
    fields, and fields derived from CUAD (contracts), MAUD or the subclass stay.
  - **`To Label – <Class>`** (insurance claims, corporate records, correspondence by default; a class with
    nothing left to label gets no sheet): one row per document with a field still to label, sorted by subclass then `document_id`. Columns, in order:
    `document_id`, `filename`, `split`, `Open original` (only when the dataset gives a source link), `Open text`,
    `subclass` (drop-down), then a value cell and a status cell for each field that needs a label on at least one
    row, then `still missing (live)` and `date flags`, then grouped, hidden helpers (`fields needing labels (as
    built)`, `dataset subclass`, `needs mask`). A document is listed
    when a field is `UNLABELED` or `pending_annotation`, or a date label is flagged `NO YEAR`,
    `NO DAY/MONTH` or `UNPARSEABLE`. Dates must include the year.
  - **`Text – <Class>`**: the full document text from the `default` config, one block per document (a
    header row with a `back to To Label` link, then the text in `part i/n` rows). Excel holds 32,767 characters per cell, so
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
  cannot be checked from a script here. The class sheets' `Hub viewer` link opens the viewer's text search for the
  filename in the `default` config (not the document_id, which that config lacks); the viewer's search index can
  take a minute to load, so treat it as best effort.
- **Drop-downs and highlights:** status, subclass and every closed-vocabulary field (intent, claim_type,
  coverage_determination, record_type, urgency, communication_type, ...) are Excel list validations (Google Sheets
  shows chips) fed from a hidden `Lists` sheet through named ranges (`lst_status`, `lst_subclass_<class>`,
  `lst_enum_<class>_<field>`). Vocabularies come from the Dojo (`extraction_schema(...).json_schema()` enums,
  `extraction_subtypes`, `RECORD_TYPES`). Yellow = a label is missing; green (conditional formatting, works in
  Excel and LibreOffice) = a person filled a gap, set a status of `verified` / `schema_documented_absence` /
  `not_applicable`, or changed the subclass. Dataset values stay unhighlighted. `verified` is the only status that
  is not a dataset code: a person sets it after checking a value, and a flagged date stays counted as missing until it
  is `verified`.
- **Formulas:** `still missing (live)`, `Labels missing` and the Summary roll-ups count status cells only
  (`SUMPRODUCT` with a column mask), use only functions Excel, LibreOffice and Google Sheets share, and follow edits.
  `fields needing labels (as built)` and `still missing (live)` agree at build time. Filter, do not sort: links between
  sheets point at fixed rows.
- **Derived fields:** `communication_type`, `cuad_family` and `merger_consideration` are derived from the subclass (the
  Dojo's own rule) and shown as populated, never as gaps. Corporate records include `filing_number` for the subclasses
  that carry it; the per-row missing set equals the Dojo's `unlabeled_fields`.
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
