# SEC corporate records

`corporate_record` · **450 rows** (13.6%) · 10 strata · train 403 / test 47 · **US public domain**.

Canonical card: [`docs/dataset-cards/s1-corporate-records.md`](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/dataset-cards/s1-corporate-records.md) in Mailroom-Corpus-EDA.

## What this block is

Exhibits extracted from **SEC EDGAR** S-1 registration statements and 8-K filings — articles of incorporation, charter amendments, bylaws, indentures, officer certificates, and related governance documents. v9 expanded the class from 39 legacy S-1 rows to **450** (+411 EDGAR exhibits).

Per-row provenance includes `exhibit_type`, `exhibit_description`, `exhibit_url`, `filer`, `accession`, and `filing_date`. `metadata.source = edgar_s1`. This is the only class with a live pointer back to the authoritative regulatory source on every row.

## Purpose in the mailroom

1. Gold `corporate_record` labels under realistic class imbalance (governance documents must not be confused with contracts or correspondence).
2. Ten subclass labels matching the pipeline taxonomy.
3. Provenance-aware evaluation (filer / accession / exhibit URL).
4. Mid-length text: EDA mean **9,462 characters** (p50 2,295; max 157,740). Comfortable in a 32k-token window for most rows; a long tail exists.

There is **no CUAD/MAUD-shaped external extraction benchmark** for this class. llm-mailroom scores a local extraction pack (`observability.local_eval_packs`) with schema-complete `expected_fields`. Extra Hub columns are joined when present, never invented.

## Strata

Largest: `charter_amendment` (80), `articles_of_incorporation` (62), `officer_certificate` (61). Smallest: `other` (3, zero test). Full table: [Classes and strata](../classes-and-strata.md#corporate_record--450-rows-136).

## Attribution

US Securities and Exchange Commission, EDGAR public filings. Works of the US federal government are in the public domain. Cite EDGAR; do not present copies as official SEC records. Re-verify against EDGAR before commercial redistribution.

## Caveats

* One filer (one S-1) can contribute multiple exhibits, so filer-level leakage between train and test is possible under a filename-hash split.
* EDGAR exhibits are .htm-derived; table and entity-escape artifacts may survive in `doc_text`.
* Mixing these public-domain rows with research-use Enron correspondence inherits Enron's stricter term for any joined redistribution.
