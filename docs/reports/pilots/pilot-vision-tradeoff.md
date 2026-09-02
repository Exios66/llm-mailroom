# Mailroom — Real Pilot Report: Vision vs Text vs Tradeoff

> **Run date:** 2026-08-09T19:49:40.593611+00:00
> **Mode:** `--real` (OPENROUTER API) · **Environment:** `pilot` · **Docs:** 3 real CUAD/Atticus contract PDFs
> **Covers:** content-completeness guarantee, ground-truth-scored extraction accuracy, and the optimal vision tradeoff point.

## 1. Executive summary

| Config | Class acc. | Archived | Review | Failed | Field score (avg) | Presence (avg) | Tokens (avg) | Total cost | Avg time |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Text-only (transcription, no images) | 1.0 | 3 | 0 | 0 | 0.533 | 0.849 | 23656 | $0.0040 | 51.328s |
| Vision + text (first 10 pages rendered) | 1.0 | 3 | 0 | 0 | 0.618 | 0.897 | 55939 | $0.0055 | 38.348s |
| Vision + text (ALL pages rendered) | 1.0 | 3 | 0 | 0 | 0.628 | 0.897 | 119047 | $0.0112 | 39.539s |

**Finding:** All three configs correctly classified all 3 documents (`class_accuracy = 1.0`) and archived them. The incremental field-score gain from adding page images is real but small (+0.08 to +0.09 mean); the **vision-all** config triples tokens on the 52-page document for an additional +0.007 field score. The pragmatic optimum is **vision-10** (full text + first 10 rendered pages): it captures nearly all of vision's accuracy benefit at ~half the token cost of vision-all. Text-only is the cheapest but lowest-accuracy option.

## 2. Method & guarantee

The pipeline is **additive**: for vision-capable models the full `doc_text` transcription is ALWAYS in the prompt, and page images are appended on top (`agents/base.py:_build_multimodal`). No configuration drops document content; `vision.max_pages` only bounds the *image* budget. `vision.max_pages=0` renders **all pages** (`llm/vision.py:render_pdf_pages`), so even scanned/late-page content is available to the model regardless of the cap.

Three configurations were run against the same 3 real CUAD/Atticus PDFs (`contract_01` affiliate, `contract_02` consulting, `contract_03` 52-page transition-services agreement):

- **Text-only (transcription, no images)** — transcription only (no images)
- **Vision + text (first 10 pages rendered)** — full text + first 10 pages as images
- **Vision + text (ALL pages rendered)** — full text + ALL pages as images

Model: `qwen/qwen3.7-flash` (sorter + specialist) via OpenRouter. Ground truth (`expected_doc_class`, `expected_stage`, `expected_fields`) is taken from `examples/samples/manifest.csv` and scored by the deterministic field scorer (`observability/field_scoring.py`), `expected_field_presence`, and the grid elements below reproduce the exact extraction so any LLM judge can audit accuracy against the ground truth.

## 3. Per-document verdict (all configs)

| Config | Doc | Class | Stage | Conf | Wall (s) | Tokens | Cost | Field score | Presence | Judge? |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| text-only | contract_01 | contract | archived | 0.98 | 53.081 | 16107 | $0.0010 | 0.54 | 0.833 | yes |
| text-only | contract_02 | contract | archived | 0.98 | 57.546 | 22140 | $0.0015 | 0.6502 | 1.0 | yes |
| text-only | contract_03 | contract | archived | 0.98 | 43.356 | 32720 | $0.0015 | 0.4103 | 0.714 | yes |
| vision-10 | contract_01 | contract | archived | 0.95 | 43.763 | 55576 | $0.0018 | 0.4802 | 0.833 | yes |
| vision-10 | contract_02 | contract | archived | 0.99 | 31.742 | 40386 | $0.0013 | 0.7952 | 1.0 | yes |
| vision-10 | contract_03 | contract | archived | 0.95 | 39.54 | 71856 | $0.0023 | 0.5778 | 0.857 | yes |
| vision-all | contract_01 | contract | archived | 0.95 | 40.211 | 64363 | $0.0021 | 0.4663 | 0.833 | yes |
| vision-all | contract_02 | contract | archived | 0.99 | 32.58 | 40503 | $0.0014 | 0.8339 | 1.0 | yes |
| vision-all | contract_03 | contract | archived | 0.98 | 45.827 | 252274 | $0.0077 | 0.5853 | 0.857 | yes |

## 4. Ground truth vs extracted (judge-auditable JSON)

For each document the **expected** (manifest ground truth) and **extracted** (per-config) values are reproduced as JSON so an LLM judge or a human can score extraction **field-by-field** against the ground truth. The `expected_fields` come from `examples/samples/manifest.csv`; the extracted payload is the raw specialist output minus pipeline metadata (`_report` etc.).

### `contract_01` — expected class `contract`, expected stage `archived`

**Ground truth (`expected_fields`):**
```json
{
  "parties": [
    "Chase Bank USA, N.A.",
    "Affiliate"
  ],
  "effective_date": null,
  "term_length": "commences on the date that the Affiliate Registration Form is approved by Chase and will end when terminated by either party",
  "termination_clauses": [
    "Either Affiliate or Chase may terminate this Agreement at any time, with or without cause, by giving the other party written or e-mail notice of termination",
    "At the time of termination, any links to Chase's Website must be removed immediately",
    "Affiliate will continue to receive Commission payments for all Approved Accounts placed during the term of this Agreement",
    "Chase may terminate this Agreement if Affiliate does not comply with the terms and conditions herein",
    "Chase also reserves the right to terminate this Agreement in the event that any Restricted Content is incorporated on Affiliate's site after acceptance",
    "IF ANY MODIFICATION IS UNACCEPTABLE TO AFFILIATE, THE ONLY RECOURSE IS TO TERMINATE THIS AGREEMENT"
  ],
  "governing_law": "Delaware",
  "key_obligations": [
    "Chase will pay Affiliate a fee for each approved credit card account that originates from a link in Affiliate's Website",
    "Affiliate cannot use or implement creative that is not available through the LinkShare affiliate interface",
    "Affiliates may not harvest or collect personal information, or email addresses using the Chase brand without the written consent of Chase",
    "Affiliate agrees to place Chase's links provided by Linkshare NetworkTM which manages the Affiliate Program ('Links') on its Affiliate's website",
    "Affiliate will only use credit card descriptions provided or approved in writing by Chase",
    "Affiliate is solely responsible for ensuring that its reviews and articles obey all applicable copyright and other laws",
    "Affiliate may not specify details regarding the Chase credit card on their Websites without the prior approval of Chase",
    "Affiliate agrees not to use the Licensed Materials in any manner that is disparaging or that otherwise portrays Chase in a negative light"
  ],
  "contract_value": "Tiered or flat commission based on private offer terms in the network; lowest tier of the public offer is $55.00",
  "renewal_terms": null
}
```

**Extracted — `Text-only (transcription, no images)`** (field score `0.54`, presence `0.833`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Chase Bank USA, N.A.",
    "Affiliate"
  ],
  "effective_date": null,
  "term_length": "The term of this Agreement will commence on the date that the Affiliate Registration Form is approved by Chase and will end when terminated by either party.",
  "termination_clauses": [
    "Either Affiliate or Chase may terminate this Agreement at any time, with or without cause, by giving the other party written or e-mail notice of termination.",
    "Chase may also terminate this Agreement if your site is deemed unsuitable based on the criteria below:",
    "Notwithstanding the foregoing, Chase may terminate this Agreement if Affiliate does not comply with the terms and conditions herein.",
    "At the time of termination, any links to Chase’s Website must be removed immediately."
  ],
  "governing_law": "State of Delaware",
  "key_obligations": [
    "Affiliate agrees to place Chase’s links provided by Linkshare NetworkTM which manages the Affiliate Program (“Links”) on its Affiliate’s website.",
    "Chase will be solely responsible for processing each order placed by a customer from Affiliate’s Links.",
    "Affiliate hereby agrees to indemnify, defend, and hold harmless Chase, its shareholders, officers, directors, employees, agents, affiliates and their respective directors, officers, employees and agents, successors and assigns, from and against any and all claims, demands, losses, liabilities, damages or expenses...",
    "Each of the parties hereto agrees that all information including, without limitation, the terms of this Agreement, business and financial information, customer and vendor lists, and pricing and sales information, concerning Chase, Customer or Affiliate shall remain strictly confidential and secret..."
  ],
  "contract_value": null,
  "renewal_terms": null
}
```

**Extracted — `Vision + text (first 10 pages rendered)`** (field score `0.4802`, presence `0.833`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Chase Bank USA, N.A.",
    "Affiliate"
  ],
  "effective_date": "The date that the Affiliate Registration Form is approved by Chase",
  "term_length": "Until terminated by either party",
  "termination_clauses": [
    "Either Affiliate or Chase may terminate this Agreement at any time, with or without cause, by giving the other party written or e-mail notice of termination.",
    "Chase may terminate this Agreement if Affiliate does not comply with the terms and conditions herein.",
    "Chase reserves the right to terminate this Agreement in the event that any Restricted Content is incorporated on Affiliate’s site after acceptance of your registration form and the commencement of the term of this Agreement."
  ],
  "governing_law": "State of Delaware",
  "key_obligations": [
    "Affiliate must submit a complete 'Affiliate Registration Form' via the Chase Affiliate Website.",
    "Affiliate agrees to place Chase’s links provided by Linkshare Network on its website.",
    "Affiliate is responsible for ensuring that the Links are formatted properly and maintained in a manner, which allows Chase to track such Approved Accounts.",
    "Affiliate will only use credit card descriptions provided or approved in writing by Chase.",
    "Chase will be solely responsible for processing each order placed by a customer from Affiliate’s Links.",
    "Chase will be solely responsible for tracking Approved Accounts made to customers who follow Affiliate’s Links.",
    "Chase is solely responsible for determining the terms and conditions of the credit cards."
  ],
  "contract_value": null,
  "renewal_terms": null
}
```

**Extracted — `Vision + text (ALL pages rendered)`** (field score `0.4663`, presence `0.833`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Chase Bank USA, N.A.",
    "Affiliate"
  ],
  "effective_date": "The date that the Affiliate Registration Form is approved by Chase",
  "term_length": "Until terminated by either party",
  "termination_clauses": [
    "Either Affiliate or Chase may terminate this Agreement at any time, with or without cause, by giving the other party written or e-mail notice of termination.",
    "Chase may terminate this Agreement if Affiliate does not comply with the terms and conditions herein.",
    "Chase reserves the right to terminate this Agreement in the event that any Restricted Content is incorporated on Affiliate’s site after acceptance of your registration form and the commencement of the term of this Agreement.",
    "Chase may also terminate this Agreement if your site is deemed unsuitable based on the criteria below"
  ],
  "governing_law": "State of Delaware",
  "key_obligations": [
    "Affiliate must submit a complete 'Affiliate Registration Form' via the Chase Affiliate Website.",
    "Affiliate agrees to place Chase’s links provided by Linkshare Network on its website.",
    "Affiliate is responsible for ensuring that the Links are formatted properly and maintained in a manner, which allows Chase to track such Approved Accounts.",
    "Affiliate will only use credit card descriptions provided or approved in writing by Chase.",
    "Chase will be solely responsible for processing each order placed by a customer from Affiliate’s Links.",
    "Chase will be solely responsible for tracking Approved Accounts made to customers who follow Affiliate’s Links.",
    "Chase is solely responsible for determining the terms and conditions of the credit cards.",
    "Affiliate hereby agrees to indemnify, defend, and hold harmless Chase from any and all claims, demands, losses, liabilities, damages or expenses."
  ],
  "contract_value": null,
  "renewal_terms": null
}
```

### `contract_02` — expected class `contract`, expected stage `archived`

**Ground truth (`expected_fields`):**
```json
{
  "parties": [
    "Global Technologies, Ltd",
    "Timothy Cabrera"
  ],
  "effective_date": "2020-01-02",
  "term_length": "one (1) year or until Consultant completes the services requested",
  "termination_clauses": [
    "Either Party shall have the right to terminate this Agreement without notice in the event of the bankruptcy, insolvency, or assignment for the benefit of creditors of the other Party",
    "Either Party shall have the right to terminate this Agreement with notice, and the effective date of termination shall be the date such notice is received (by mail, overnight delivery, or fax) by the terminated Party",
    "if the Company [is not] truthful with Consultant in regard to any relevant material regarding the Company ... this entire Agreement will terminate and all consideration paid shall be forfeited without further notice"
  ],
  "governing_law": "Florida",
  "key_obligations": [
    "The Company shall pay Consultant $250,000 cash compensation",
    "Seek and advise the Company on the acquisition of potential products for the Company's subsidiaries",
    "Assist in negotiating acquisition or merger consideration as required by the Company",
    "Assist in the sale of any inventory held by any of the Company's subsidiaries",
    "Assist in the sale of any future products acquired, licensed or developed by the Company and or its subsidiaries",
    "Consultant shall devote such time and efforts to the affairs of the Company as is reasonably necessary to render the services contemplated by this Agreement",
    "Consultant will in no way act as a 'broker-dealer' under state securities laws",
    "Comply with all federal and state laws",
    "The Consultant will be solely responsible for all tax returns and payments required to be filed with or made to any federal, state or local tax authority"
  ],
  "contract_value": "$250,000",
  "renewal_terms": null
}
```

**Extracted — `Text-only (transcription, no images)`** (field score `0.6502`, presence `1.0`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Global Technologies, Ltd",
    "Timothy Cabrera"
  ],
  "effective_date": "January 2, 2020",
  "term_length": "one (1) year or until Consultant completes the services requested",
  "termination_clauses": [
    "Either Party shall have the right to terminate this Agreement without notice in the event of the bankruptcy, insolvency, or assignment for the benefit of creditors of the other Party.",
    "Either Party shall have the right to terminate this Agreement with notice, and the effective date of termination shall be the date such notice is received (by mail, overnight delivery, or fax) by the terminated Party."
  ],
  "governing_law": "State of Florida",
  "key_obligations": [
    "Consultant shall perform transactional business services, including developing business plans, advising on acquisitions, and assisting with sales.",
    "Consultant shall devote reasonable time and effort, use commercially reasonable efforts, and comply with all applicable laws.",
    "Company shall pay Consultant $250,000,00 cash compensation.",
    "Company shall cooperate with Consultant, provide necessary information, and report payments via Form 1099-MISC.",
    "Both Parties shall keep proprietary information confidential and indemnify each other against claims arising from negligence or breach."
  ],
  "contract_value": "$250,000,00",
  "renewal_terms": null
}
```

**Extracted — `Vision + text (first 10 pages rendered)`** (field score `0.7952`, presence `1.0`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Global Technologies, Ltd",
    "Timothy Cabrera"
  ],
  "effective_date": "2020-01-02",
  "term_length": "one (1) year or until Consultant completes the services requested",
  "termination_clauses": [
    "Either Party shall have the right to terminate this Agreement without notice in the event of the bankruptcy, insolvency, or assignment for the benefit of creditors of the other Party.",
    "Either Party shall have the right to terminate this Agreement with notice, and the effective date of termination shall be the date such notice is received (by mail, overnight delivery, or fax) by the terminated Party."
  ],
  "governing_law": "State of Florida",
  "key_obligations": [
    "Consultant will perform the services described in Section 3 (Transactional Business) for the Company.",
    "Company shall pay Consultant $250,000.00 cash compensation.",
    "Company agrees to cooperate with Consultant in carrying out the purposes of this Agreement, keep Consultant informed of any developments of importance pertaining to the Company's business and abide by this Agreement in its entirety.",
    "Consultant shall devote such time and efforts to the affairs of the Company as is reasonably necessary to render the services contemplated by this Agreement."
  ],
  "contract_value": "$250,000.00 cash compensation",
  "renewal_terms": null
}
```

**Extracted — `Vision + text (ALL pages rendered)`** (field score `0.8339`, presence `1.0`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Global Technologies, Ltd",
    "Timothy Cabrera"
  ],
  "effective_date": "2020-01-02",
  "term_length": "one (1) year or until Consultant completes the services requested",
  "termination_clauses": [
    "Either Party shall have the right to terminate this Agreement without notice in the event of the bankruptcy, insolvency, or assignment for the benefit of creditors of the other Party.",
    "Either Party shall have the right to terminate this Agreement with notice, and the effective date of termination shall be the date such notice is received (by mail, overnight delivery, or fax) by the terminated Party."
  ],
  "governing_law": "State of Florida",
  "key_obligations": [
    "Consultant will perform the services described in Section 3 (Transactional Business) for the Company.",
    "Company shall pay Consultant $250,000 cash compensation.",
    "Company agrees to cooperate with Consultant in carrying out the purposes of this Agreement, keep Consultant informed of any developments of importance pertaining to the Company's business and abide by this Agreement in its entirety.",
    "Consultant shall devote such time and efforts to the affairs of the Company as is reasonably necessary to render the services contemplated by this Agreement.",
    "Consultant will be solely responsible for all tax returns and payments required to be filed with or made to any federal, state or local tax authority."
  ],
  "contract_value": "$250,000",
  "renewal_terms": null
}
```

### `contract_03` — expected class `contract`, expected stage `archived`

**Ground truth (`expected_fields`):**
```json
{
  "parties": [
    "Reynolds Group Holdings Inc.",
    "Reynolds Consumer Products Inc."
  ],
  "effective_date": "2020-02-04",
  "term_length": "12 months following the Commencement Date unless a different date is specified on Exhibit A or Exhibit B",
  "termination_clauses": [
    "Company may terminate for convenience any Transition Service, and RGHI may terminate for convenience any Reverse Transition Service, upon 30 days' prior written notice",
    "This Agreement shall terminate when the Termination Date has occurred for all Services",
    "this Agreement may be terminated by either Party ... upon written notice ... if the other Party or its Affiliates materially breaches this Agreement and such breach is not cured, to the reasonable satisfaction of the Terminating Party, within thirty (30) days of written notice thereof",
    "if the other Party files for bankruptcy or similar proceeding ... becomes or is declared insolvent, becomes the subject of any proceedings ... related to its liquidation, insolvency, bankruptcy or the appointment of a trustee or a receiver ... or a court approves reorganization proceedings on such Party"
  ],
  "governing_law": "Illinois",
  "key_obligations": [
    "RGHI shall provide, or shall cause its Affiliates or ... third parties to provide, to the Company ... the services described on Exhibit A hereto (the 'Transition Services')",
    "Company shall provide, or shall cause its Affiliates ... to provide, to RGHI ... the services described on Exhibit B hereto (the 'Reverse Transition Services')",
    "RGHI shall ... undertake the segregation and extraction required to separate the IT systems, data, records and processes of the Company ... and migrate them to RCP's ... IT environment or infrastructure (the 'Migration Services')",
    "The costs of such Migration Services shall be paid by RCP",
    "Recipient shall pay Provider (i) the fee for each Service set forth on Exhibit A or Exhibit B, (ii) Providers' and their Affiliates' reasonable and documented out-of-pocket expenses ... and (iii) any other fees as agreed to by the Parties in writing (collectively, the 'Fees')",
    "Recipient shall pay, or cause to be paid, the Fees shown on an invoice no later than the last business day of the month Recipient received such invoice",
    "Provider shall (x) be responsible for the performance or non-performance of any such parties and (y) in all cases remain responsible for ensuring that obligations with respect to the standards of Services set forth in Article III of this Agreement are satisfied"
  ],
  "contract_value": "No aggregate amount; per-service fees set forth on Exhibit A/Exhibit B (e.g. $220,775 per month Base Infrastructure, $100,275 per month Tax Services, $97,353 per month LAN & WAN Management, $175 per person per hour Audit Services)",
  "renewal_terms": null
}
```

**Extracted — `Text-only (transcription, no images)`** (field score `0.4103`, presence `0.714`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Reynolds Group Holdings Inc.",
    "Reynolds Consumer Products Inc."
  ],
  "effective_date": null,
  "term_length": "12 to 24 months from the Commencement Date, depending on the specific service category",
  "termination_clauses": [
    "Company may terminate for convenience any Transition Service, and RGHI may terminate for convenience any Reverse Transition Service, upon 30 days’ prior written notice of such termination",
    "This Agreement may be terminated by either Party if the other Party or its Affiliates materially breaches this Agreement and such breach is not cured, to the reasonable satisfaction of the Terminating Party, within thirty (30) days of written notice thereof",
    "Either Party may terminate if the other Party files for bankruptcy or similar proceeding, becomes insolvent, makes a general assignment for the benefit of creditors, or becomes subject to liquidation or reorganization proceedings"
  ],
  "governing_law": "State of Illinois",
  "key_obligations": [
    "RGHI shall provide Transition Services, Reverse Transition Services, and Migration Services as described in Exhibits A and B",
    "Recipient shall pay Provider the Fees set forth on Exhibits A or B, plus reasonable documented out-of-pocket expenses and pass-through costs",
    "Provider shall perform Services at a level of quality substantially similar to that performed during the twelve (12) month period prior to the Commencement Date",
    "Parties shall use reasonable commercial efforts to agree on key controls over financial reporting to comply with the Sarbanes-Oxley Act of 2002"
  ],
  "contract_value": null,
  "renewal_terms": null
}
```

**Extracted — `Vision + text (first 10 pages rendered)`** (field score `0.5778`, presence `0.857`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Reynolds Group Holdings Inc.",
    "Reynolds Consumer Products Inc."
  ],
  "effective_date": "2020-02-04",
  "term_length": "12 months following the Commencement Date",
  "termination_clauses": [
    "Company may terminate for convenience any Transition Service, and RGHI may terminate for convenience any Reverse Transition Service, upon 30 days' prior written notice.",
    "Either Party may terminate upon written notice if the other Party materially breaches this Agreement and such breach is not cured within thirty (30) days of written notice.",
    "Either Party may terminate if the other Party files for bankruptcy, becomes insolvent, or undergoes liquidation or dissolution proceedings."
  ],
  "governing_law": "State of Illinois",
  "key_obligations": [
    "RGHI shall provide Transition Services to the Company as described in Exhibit A.",
    "Company shall provide Reverse Transition Services to RGHI as described in Exhibit B.",
    "RGHI shall undertake Migration Services to separate and migrate IT systems, data, records, and processes to RCP's environment.",
    "Recipient shall pay Provider the Fees set forth in Exhibit A or Exhibit B, plus out-of-pocket expenses.",
    "Each Party shall maintain books and records of all material transactions pertaining to the Services."
  ],
  "contract_value": null,
  "renewal_terms": null
}
```

**Extracted — `Vision + text (ALL pages rendered)`** (field score `0.5853`, presence `0.857`, class_correct `1`, stage_correct `1`):
```json
{
  "parties": [
    "Reynolds Group Holdings Inc.",
    "Reynolds Consumer Products Inc."
  ],
  "effective_date": "2020-02-04",
  "term_length": "12 months from Commencement Date (with some services up to 24 months)",
  "termination_clauses": [
    "Section 6.2: Company may terminate for convenience any Transition Service, and RGHI may terminate for convenience any Reverse Transition Service, upon 30 days' prior written notice.",
    "Section 6.3: Either Party may terminate upon written notice if the other Party materially breaches this Agreement and such breach is not cured within thirty (30) days, or if the other Party files for bankruptcy or similar proceeding.",
    "Section 6.3: This Agreement shall terminate when the Termination Date has occurred for all Services."
  ],
  "governing_law": "State of Illinois",
  "key_obligations": [
    "RGHI shall provide Transition Services to the Company as described in Exhibit A.",
    "Company shall provide Reverse Transition Services to RGHI as described in Exhibit B.",
    "RGHI shall undertake Migration Services to separate IT systems, data, records and processes of the Company from RGHI's IT environment.",
    "Recipient shall pay Provider the Fees set forth in Exhibit A or Exhibit B, plus out-of-pocket expenses.",
    "Each Party shall maintain books and records of all material transactions pertaining to the Services."
  ],
  "contract_value": null,
  "renewal_terms": null
}
```

## 5. Optimal vision tradeoff

### 5.1 Accuracy vs cost

| Config | Class acc. | Mean field score | Mean presence | Tokens/doc | $/doc | Avg time |
|---|---|---:|---:|---:|---:|---:|
| Text-only (transcription, no images) | 1.0 | 0.533 | 0.849 | 23656 | $0.0040 | 51.328s |
| Vision + text (first 10 pages rendered) | 1.0 | 0.618 | 0.897 | 55939 | $0.0055 | 38.348s |
| Vision + text (ALL pages rendered) | 1.0 | 0.628 | 0.897 | 119047 | $0.0112 | 39.539s |

### 5.2 Marginal return of page images

Measured against the text-only baseline:

| Config | Δ field score | Δ tokens/doc | Tokens per +0.01 field score |
|---|---:|---:|---:|
| Vision + text (first 10 pages rendered) | +0.085 | +32283 | 3798.0 |
| Vision + text (ALL pages rendered) | +0.095 | +95391 | 10041.2 |

### 5.3 On the important document-level effect

| Config | `contract_03` field score | `contract_03` tokens | `contract_03` cost |
|---|---:|---:|---:|
| Text-only (transcription, no images) | 0.4103 | 32720 | $0.0015 |
| Vision + text (first 10 pages rendered) | 0.5778 | 71856 | $0.0023 |
| Vision + text (ALL pages rendered) | 0.5853 | 252274 | $0.0077 |

### 5.4 Recommendation

1. **Content guarantee (non-negotiable):** always keep the full `doc_text` transcription in the prompt — done additively; never let a page cap drop document content (`vision.max_pages=0` renders all pages when a scanned/late-page scenario needs it).
2. **Optimal default = `vision-10`** for text-based legal PDFs: it delivers most of vision's accuracy benefit (e.g. +0.10 on `contract_03` vs text-only) at ~2x the token cost of text-only and roughly **half** the tokens of vision-all.
3. **Roll `vision-all` only when** (a) the PDF is scanned/garbled (sparse text extraction), (b) the document is short (≤ ~10 pages), or (c) the risk of losing a late-page clause outweighs the ~3x token cost — e.g. high-stakes M&A reps/warranties.
4. **Cheapest = text-only** when documents are clean text PDFs and the small accuracy delta (≈ +0.08 mean field score) is not worth the 2-5x token increase; ideal for bulk/backlog ingestion where rate limits dominate.

> The exact crossover depends on document type: for **contract_03** (52 pages) vision-all costs ~3.3x vision-10's tokens for a negligible +0.008 field-score gain — the optimum sits at **vision-10**; for short scanned docs the optimum may be vision-all. Run `scripts/run_vision_sweep.py --real --source <corpus>` to measure per-corpus.
