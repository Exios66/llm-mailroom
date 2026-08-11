# Sample Attribution & Licenses

The pilot sample set mixes **real public-domain / CC-BY-4.0 documents** with
**original text written for this repo**. Every file is tracked in
`docs/examples/samples/manifest.csv` (columns `source` + `license`).

## Real contracts — CUAD / The Atticus Project

The contract PDFs under `docs/examples/samples/contract/` come from the
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
- `atticus_01_ip_agreement.pdf` — Ingevity Corp, EX-10.5 (Intellectual Property Agreement)
- `atticus_02_license_agreement.pdf` — Artara Therapeutics, EX-10.5 (License Agreement)
- `atticus_03_supply_agreement.pdf` — Loha Company Ltd, Form F-1, EX-10.16 (Supply Agreement)
- `atticus_04_franchise_agreement.pdf` — Buffalo Wild Wings Inc, EX-10.3 (Franchise Agreement)
- `atticus_05_distributor_agreement.pdf` — Netgear Inc, EX-10.16 (Distributor Agreement)
- `atticus_06_joint_venture_agreement.pdf` — Accelerated Technologies Holding Corp, EX-10.13 (Joint Venture Agreement)

The last six are fetched by `scripts/fetch_external_samples.py --source atticus`.

## LegalBench — MAUD v1 merger agreements

The six `legalbench_*.txt` files under `docs/examples/external/legalbench/` are the
**full merger agreement texts behind LegalBench's 34 `maud_*` tasks**
(see [LegalBench](https://github.com/HazyResearch/legalbench) and the
[MAUD paper](https://arxiv.org/abs/2301.00876)). They come from **MAUD v1**
(*Merger Agreement Understanding Dataset*, curated by The Atticus Project),
available at Zenodo: **DOI 10.5281/zenodo.7500064**.

- License: **CC BY 4.0** (per the MAUD v1 Zenodo record and paper appendix A.1).
- The agreements are SEC-sourced public merger agreements (2021 ABA Public
  Target Deal Points Study corpus).
- Attribution: *The Atticus Project — MAUD v1*,
  `10.5281/zenodo.7500064`, accessed 2026; task framing via
  `nguha/legalbench` (CC BY 4.0).
- Fetched by `scripts/fetch_external_samples.py --source legalbench`.

## Pile of Law — court opinions (public-domain subsets only)

The six `pileoflaw_*.txt` files under `docs/examples/external/pileoflaw/` are U.S.
court opinions from the **`courtlistener_opinions`** subset of
[**Pile of Law**](https://huggingface.co/datasets/pile-of-law/pile-of-law).

- The **Pile of Law compilation is CC BY-NC-SA 4.0** and is deliberately
  **not** committed; only **public-domain U.S. government works** (court
  opinions) are sampled and redistributed here — per the Pile of Law paper
  (arXiv:2207.00220, appendix E), all U.S. government-generated content is
  public domain, and these opinions were originally published by U.S. courts
  via CourtListener.
- License: **public domain (U.S. government work)**.
- Attribution: *Pile of Law* `pile-of-law/pile-of-law` on Hugging Face
  (`courtlistener_opinions` subset), accessed 2026; opinions sourced from
  CourtListener (per-document URLs in each file header).
- Fetched by `scripts/fetch_external_samples.py --source pileoflaw`.

## Synthetic PDFs — original text

The PDFs for compliance, corporate, correspondence, and due diligence classes are
generated from original `.txt` text under `docs/examples/sources/` by
`scripts/prepare_samples.py` (ReportLab). The text is written for this project
and is not copied from any dataset; it is styled after common document types
(10-K annual report, state filing, bylaws, board resolution, demand letter,
internal memo, due diligence report/checklist).

- License: original; free to use within this repository.
- No attribution required.
