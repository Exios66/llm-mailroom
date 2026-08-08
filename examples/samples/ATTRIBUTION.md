# Sample Attribution & Licenses

The pilot sample set mixes **real public-domain/CC-BY-4.0 documents** with
**original text written for this repo**. Every file is tracked in
`examples/samples/manifest.csv` (columns `source` + `license`).

## Real contracts — CUAD / The Atticus Project

The three contract PDFs under `examples/samples/contract/` come from the
[**Contract Understanding Atticus Dataset (CUAD)**](https://huggingface.co/datasets/theatticusproject/cuad),
maintained by The Atticus Project (see the [CUAD datasheet](https://arxiv.org/abs/2103.06268)).

- License: **CC BY 4.0** (per the HF dataset card for `theatticusproject/cuad`).
- The contracts are SEC filing exhibits (S-1/8-K/10-K) and were already public
  filings; CUAD republishes them under CC BY 4.0.
- Attribution: *The Atticus Project — CUAD v1*,
  `theatticusproject/cuad` on Hugging Face, accessed 2026.
- Do not redistribute beyond the terms of CC BY 4.0.

Files:
- `contract_01_affiliate_agreement.pdf` — CreditcardscomInc, Form S-1, EX-10.33 (Affiliate Agreement)
- `contract_02_consulting_agreement.pdf` — Global Technologies Ltd, EX-10.16 (Consulting Agreement)
- `contract_03_service_agreement.pdf` — Reynolds Consumer Products Inc, Form S-1/A, EX-10.22 (Transition Services Agreement)

## Synthetic PDFs — original text

The PDFs for compliance, corporate, correspondence, and due diligence classes are
generated from original `.txt` text under `examples/sources/` by
`scripts/prepare_samples.py` (ReportLab). The text is written for this project
and is not copied from any dataset; it is styled after common document types
(10-K annual report, state filing, bylaws, board resolution, demand letter,
internal memo, due diligence report/checklist).

- License: original; free to use within this repository.
- No attribution required.

## Not used

- **Pile of Law** (`pile-of-law/pile-of-law`) is **CC BY-NC-SA 4.0** and was
  deliberately **not** committed. It was only used as a source reference.
- **LegalBench** (`nguha/legalbench`) is CC BY 4.0 and is not included; it is a
  candidate for a future clause-QA evaluation layer.
