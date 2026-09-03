# Agent Specifications

## Agent Architecture

All agents inherit from `agents/base.py:BaseAgent` and share a common interface:

```python
class BaseAgent(ABC):
    agent_name: str               # Must match key in config/taxonomy.yaml agents:

    def __init__(self):
        self.client, self.model = get_llm(self.agent_name)

    @abstractmethod
    def system_prompt(self) -> str: ...

    def _call_llm(self, user_message, response_format=None, temperature=None,
                  max_tokens=None, system_prompt=None) -> str: ...

    def _call_structured(self, user_message, json_schema, temperature=0.1,
                         system_prompt=None) -> dict: ...
```

Key design points:
- `self.client` and `self.model` are resolved from `config/taxonomy.yaml` → `llm/providers.py` → `llm/client.py`
- `system_prompt()` fetches the **Langfuse-managed prompt** (`mailroom-<agent_name>`, production label) via `llm/prompts.py:get_managed_prompt`, falling back to the identical template shipped in code when Langfuse is unavailable — behavior never depends on the observability backend being up. Sync templates with `scripts/sync_prompts.py`.
- `_call_structured()` uses `response_format={"type": "json_object"}` and appends boilerplate that guarantees the literal token `json` in the messages (some providers reject requests without it) and embeds the JSON schema in the prompt.
- Every LLM call goes through `llm/retry.py:retry_chat_completion` (transient failures only: connection errors, timeouts, 429, 5xx) and a `max_tokens` cap from the agent's `taxonomy.yaml` entry.
- When a managed prompt is active, it's passed to the OpenAI call as `langfuse_prompt=`, linking each generation to its exact prompt version in the trace UI.
- Every agent has a distinct system prompt ("personality") aligned with its role

**Two of the agents — the Sorter and the Contracts Specialist — are vendored LangChain agents** (from `github.com/Exios66/llm-entity-extraction`, kept in sync with that repo's append-only prompt lineage — re-vendored to the sibling's current HEAD on 2026-08-15), imported into `langchain_agents/` with mailroom plumbing adapted in (pages/vision, run-deadline checks, per-call usage accounting — each adaptation marked `MAILROOM PATCH`). They use `langchain-openai`'s `ChatOpenAI` + `with_structured_output` instead of the mailroom's `agents/base.py` plumbing, and their system prompts resolve **by version key** through `langchain_agents/prompts.py:PROMPT_VERSIONS`: the production aliases are `"sorter"` → `SORTER_PROMPT_V14` (V12 CUAD-subtype lineage + mailroom pipeline doctrine; V13 remains a frozen insurance-class experiment derived from V0) and `"contracts_specialist"` → `CONTRACTS_SPECIALIST_PROMPT_V33` (V32 + pared checklist doctrine — no open-ended `key_obligations` / `termination_clauses`). The full eval history rides along in-repo (`sorter_v0…v14`, vision v0–v1, `contracts_specialist_v1…v33`) so evaluation loops can pin exactly one version per experiment — they bypass `get_managed_prompt`/Langfuse prompt linking (generations are still auto-traced via the langfuse-openai SDK patch). All other agents follow the `BaseAgent` contract below.

---

## Agent Roster

### 1. Sorter (`agents/sorter.py`)

| Attribute | Value |
|---|---|
| **Node** | `classify`, `retry_classify` |
| **Trigger** | New document in `/processing` |
| **Input** | Raw document text (+ page images for vision-capable models) |
| **Output** | `doc_type` + `contract_subtype` + `confidence` + `reasoning` |
| **Personality** | Fast, decisive, flags ambiguity instead of guessing |

**System prompt seed:** "You are a fast, decisive legal document classifier operating in a transactional/corporate law firm's mailroom."

The Sorter is the first LLM call in the pipeline. It reads the document text and determines which of the configured document classes it belongs to. The list of available classes is dynamically read from `config/taxonomy.yaml`, so adding a new document type automatically expands the Sorter's options.

The Sorter is a **vendored LangChain agent** (`agents/sorter.py` re-exports `langchain_agents.sorter_agent.SorterAgent`): it classifies via `with_structured_output` against the `SORTER_SCHEMA`, uses the production `sorter_v14` prompt (V12 CUAD-subtype lineage + mailroom pipeline doctrine), and adds a **contract-subtype dimension** — for contracts it assigns one of 25 CUAD agreement families (affiliate, license, distributor, franchise, …) plus `other` (`CONTRACT_SUBTYPE_KEYS`, normalized via `normalize_subtype`; non-contracts carry `contract_subtype=None`). `classify()` returns a 4-tuple `(doc_type, contract_subtype, confidence, reasoning)`; the subtype flows into state, the classification guard, the extraction handoff context, the report, and the catalog. **No-truncation doctrine (HUB-038):** the mailroom subclass bypasses the upstream HEAD+TAIL truncation — documents past the input budget are classified in overlapping sliding windows (every character read) and merged deterministically (plurality vote among non-unknown classes, mean confidence of agreeing windows, first non-null subtype/subclass, joined reasoning; `WINDOW i OF n` markers per call). The advisory intake read rides every window as a labeled prior; page images attach to the first window only.

---

### 1b. Sorter Reviewer (`agents/sorter_reviewer.py`)

| Attribute | Value |
|---|---|
| **Node** | `review_classify` (Lane A) |
| **Trigger** | Medium-band classification that survived `retry_classify` |
| **Input** | Document text (+ page images); **blind** to the sorter's answer |
| **Output** | Independent `doc_type` + `contract_subtype` + `doc_subclass` + `confidence` |
| **Personality** | Independent second opinion; agreement is computed by the graph, not the model |

Fires only where the pipeline would previously have pinged a human. Independence is the point: the reviewer never sees the sorter's label. The graph node compares the two opinions and either applies the reviewer's class or escalates to human review.

---

### 2. Contracts Specialist (`agents/contracts_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type` is `contract` (CUAD) or `merger_agreement` (MAUD) |
| **Input** | Contract text + `ContractExtraction` schema (+ page images) |
| **Output** | Structured extraction + confidence |
| **Personality** | Meticulous, formal, precise to a fault |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `document_name` | `str \| None` | The name of the contract |
| `parties` | `list[str]` | All named parties |
| `effective_date` | `str \| None` | Contract effective date |
| `term_length` | `str \| None` | Duration |
| `governing_law` | `str \| None` | Governing jurisdiction |
| `contract_value` | `str \| None` | Total value / MAUD consideration token |
| `renewal_terms` | `str \| None` | Renewal conditions |
| `cuad_family` | `str \| None` | CUAD agreement family |
| `merger_consideration` | `str \| None` | MAUD consideration token |
| `cuad_clauses` | `list[str]` | Present CUAD categories as `"<label>: <evidence>"` |
| `maud_clauses` | `list[str]` | Answered MAUD questions as `"<question>: <evidence>"` |

The Contracts Specialist is also a **vendored LangChain agent** (`agents/contracts_specialist.py` re-exports `langchain_agents.specialist_agents.ContractsSpecialist`): production prompt `contracts_specialist_v33` (pared CUAD/MAUD checklists — no open-ended `key_obligations` / `termination_clauses`), `normalize_extraction` guarantees every schema field is present, and a missing `confidence` is derived from the share of fields actually found. It extracts **two live classes** that share `ContractExtraction`: CUAD `contract` and MAUD `merger_agreement` (taxonomy `specialist: contracts_specialist`; they are not interchangeable labels). It accepts a **`handoff_context`** — the chained-eval pattern: the graph passes the sorter's classification (`doc_type` + `contract_subtype` / MAUD consideration + confidence) into the extraction call so the specialist extracts with the expected clause set of that agreement family in mind. The other four specialists accept the same optional `handoff_context` parameter.

---

### 3. Corporate Records Specialist (`agents/corporate_records_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == corporate_record` |
| **Input** | Document text + `CorporateRecordExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Methodical, loves structure and hierarchy |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `entity_name` | `str` | Legal entity name |
| `record_type` | `str` | Hub extract tokens: `articles_of_incorporation`, `bylaws`, `powers_of_attorney`, `rights_instrument`, `other` (sorter catalog is wider) |
| `effective_date` | `str \| None` | Date the record took effect |
| `intent` | `str \| None` | Short controlled label (e.g. `record_governance`) |
| `subject_matter` | `str \| None` | One grounded sentence |
| `keywords` | `list[str]` | Up to 8 grounded terms |
| `signatories` | `list[str]` | Who signed/approved |
| `jurisdiction` | `str \| None` | State/country of incorporation |
| `filing_number` | `str \| None` | Official filing reference |

**Honest gap (dojo 0.11.0):** there is **no external extraction benchmark** for this class (nothing CUAD/MAUD-shaped). The published `mailroom-corpus` set has 39 `corporate_record` rows with record-type subclasses; Hub extract inventory stays the five tokens above — do not treat those 39 rows as clause-level gold. Mailroom scores a **local extraction pack** (`observability.local_eval_packs`, mock/check only) with schema-complete `expected_fields` (entity_name, subject_matter, keywords, signatories, …) from committed fixtures. Extra Hub `ground_truth` columns are joined when present, never invented.

---

### 4. Correspondence Specialist (`agents/correspondence_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == correspondence` |
| **Input** | Document text + `CorrespondenceExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Reads between the lines, tracks narrative/intent |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `sender` | `str` | Who sent it |
| `recipient` | `str` | Who received it |
| `additional_recipients` | `list[str]` | Cc'd / copied parties |
| `communication_type` | `str` | letter, email, memo, notice, demand, etc. |
| `communication_date` | `str \| None` | When it was sent |
| `intent` | `str \| None` | Short controlled label (e.g. `demand_payment`) |
| `subject_matter` | `str \| None` | One grounded sentence |
| `keywords` | `list[str]` | Up to 8 grounded terms |
| `demand_amount` | `float \| None` | Exact dollar amount demanded (demand letters) |
| `action_items` | `list[str]` | At most 3 concrete actions |
| `urgency` | `str` | routine, time-sensitive, urgent, critical |
| `confidence` | `float` | Extraction confidence (evidence-derived) |

---

### 5. Compliance Specialist (`agents/compliance_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == compliance_filing` |
| **Input** | Document text + `ComplianceFilingExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Rule-bound, cites authority, cautious |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `filing_type` | `str` | SEC filing type, state filing, etc. |
| `regulatory_body` | `str` | SEC, state secretary, IRS, etc. |
| `filing_date` | `str \| None` | When filed |
| `due_date` | `str \| None` | Statutory deadline |
| `entity_name` | `str` | Filing entity |
| `key_requirements` | `list[str]` | Regulatory obligations satisfied |
| `status` | `str \| None` | draft, filed, pending, overdue |
| `reference_number` | `str \| None` | Accession/tracking number |

**Honest gap (dojo 0.11.0):** `compliance_filing` has **zero rows** in `Lucius-Morningstar/mailroom-corpus`. The Hub SEC form-body inventory (`10-K`, `10-Q`, `8-K`, …) is the live subclass catalog; the suite scores typed extraction plus that inventory. The HF pilot (`scripts/run_hf_pilot.py`) therefore omits this class from Hub `--real` — it must not report a corpus accuracy at n=0. A **local pack** of committed fixtures (10-K + state filing) is scored on `--check` / `--mock` only.

---

### 6. Insurance Claims Specialist (`agents/insurance_claims_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == insurance_claim` |
| **Input** | Document text + `InsuranceClaimExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Detail-driven claims analyst — documents what the file shows, never argues with it |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `claim_number` | `str \| None` | Claim reference number |
| `policy_number` | `str \| None` | Policy the claim is filed under |
| `insurer` | `str` | Insurance carrier |
| `insured_party` | `str` | Policyholder / insured party |
| `claim_type` | `str` | CMS/DE-SynPUF `pde` / `inpatient` / `outpatient` / `carrier`, plus legacy FNOL lines (`auto`, `property`, `liability`, `health`, `life`, `workers_comp`, `other`) |
| `date_of_loss` | `str \| None` | Date of the loss event |
| `date_filed` | `str \| None` | Date the claim was filed |
| `claimed_amount` | `float \| None` | Amount claimed |
| `adjuster` | `str \| None` | Assigned adjuster; null is valid for CMS / DE-SynPUF rows |
| `damages_description` | `str` | Damages narrative |
| `coverage_determination` | `str` | approved, denied, partial, pending |
| `denial_reasons` | `list[str]` | Stated denial reasons |
| `supporting_documents` | `list[str]` | Documents referenced as supporting the claim |
| `intent` | `str \| None` | Short controlled label (e.g. `coverage_denial`) |
| `subject_matter` | `str \| None` | One grounded sentence |
| `keywords` | `list[str]` | Up to 8 grounded terms |
| `claim_checklist` | `list[str]` | Present claim categories as `"<Category>: <evidence>"` |
| `confidence` | `float` | Extraction confidence (evidence-derived) |

A first-class document class (added in mailroom v0.4.0 / KANBAN-067): schema registry, taxonomy doc_class + agent block, graph dispatch node, classifier vocabulary, and sorter prompt coverage.

**Honest gap (dojo 0.11.0):** Hub rows are CMS DE-SynPUF source tables (`carrier`/`inpatient`/`outpatient`/`pde`). Typed extraction plus field-micro P/R/F1/F2 are scored. **`determination_consistency` and `amount_exactness` are registered scorers**; CMS GT is homogeneous (all `coverage_determination=approved` with empty `denial_reasons`), so Hub `determination_consistency` is **gated** (not a quality KPI on GT-shaped rows). A local contrast pack (approved / denied / partial) exercises the scorer off that tautology. The same three determinations also live on the pilot manifest as synthetic mock-only PDFs (`insurance_01` approved / `insurance_02` denied / `insurance_03` partial, rendered from `docs/examples/sources/insurance/` by `prepare_samples.py`) so `--mock` pilots cover `insurance_claim` end-to-end; `--real` refuses them via `is_real_sample`. Mailroom still records a local field invariant on traces. Candidate corpus EDA lives in [`claims-data-eda`](https://github.com/Exios66/claims-data-eda).

---

### Retired classes (`court_opinion`, `due_diligence`)

Retired from the live pipeline in v0.5.0 / PR #21. The sorter emits `unknown` (human review); there is no specialist dispatch, extraction schema, or managed prompt. Dojo keeps historical suites with `retired=True` (`list_suites(live_only=True)` excludes them). **Court opinions:** LegalBench remains the real benchmark surface. **Due diligence:** zero rows in `mailroom-corpus`.

---

### 7. Report assembler (`agents/reporter.py`)

| Attribute | Value |
|---|---|
| **Node** | `compile_report` |
| **Trigger** | Extraction complete, confidence sufficient (or arbiter `accept_with_caveats`) |
| **Input** | Specialist extraction + optional arbiter caveats |
| **Output** | Deterministic `_report` string on `extracted_data` |
| **Personality** | none — procedural |

Happy-path LLM calls stop at classify + extract. `compile_report` is a **procedural** assembler (no `get_llm("reporter")`): it formats the specialist JSON plus any durable arbiter caveats into `extracted_data._report`. Archivist remains the success-path durable sink.

---

### 8. Archivist (`agents/archivist.py`)

| Attribute | Value |
|---|---|
| **Node** | `archive` |
| **Trigger** | Report compiled |
| **Input** | Full manifest + file path |
| **Output** | Archive path + audit log entry |
| **Personality** | Quiet, exhaustive, never skips a step |

The Archivist is NOT an LLM agent — it's a procedural function that:

1. Moves the file to `/archive/<matter_id>/<doc_type>/`
2. Writes the manifest as a JSON sidecar
3. Creates a hash-chained audit log entry

---

### 8b. Intake clerk (`agents/intake.py`)

| Attribute | Value |
|---|---|
| **Node** | the first-class `intake` node — the intake agent IS the ingest specialist (the ingest + intake steps are ONE; span `normalize-intake` + `intake-llm-prep`) |
| **Trigger** | every document after text extraction (deterministic core); LLM pass gated to messy / over-sorter-budget documents |
| **Input** | transcribed `doc_text` |
| **Output** | cleaned text + stats (`messy`, `changed`, hyphen unwraps, collapsed blanks); LLM pass: advisory triage read + section map + optional structural cleaning |
| **Personality** | the intake clerk — first agent in the pipeline, one fused TRIAGE + CLEAN + PREPARE pass |

The deterministic clerk (dojo `llm_dojo_scoring.intake` gold, re-exported
byte-compatible) is the MANDATORY baseline and never skipped: Unicode NFC,
newline/NBSP/zero-width/C0 cleanup, hyphen unwrap, blank collapse, trim,
`looks_messy`. The-Mailroom mirrors `deterministic_normalize` /
`looks_messy` in `mailroom_ui/intake_normalize.py` and reads the span on every
`document-pipeline` trace; Hugging Face pilots depend on it.

**LLM-assisted pass (HUB-038).** On top of the clerk, an LLM pass (ONE fused
call per window) TRIAGES, CLEANS, and PREPARES the document for the
classification and extraction agents:

- **Triage** — an advisory first read (primary doc class + subclass +
  confidence + gist + keywords), the same vocabulary-clamped shape as the
  free triage team's `validate_triage`. It rides the terminal manifest's
  `intake.triage`, the completion echo's INTAKE TRIAGE section, and is fed to
  the sorter as a labeled prior — the sorter re-classifies independently;
  intake NEVER overrules it.
- **Clean** — structural repair of messy OCR-ish text (join run-together
  lines, drop repeated header/footer artifacts; never alters facts). The
  model's output is re-run through the deterministic clerk so
  `prep_invariants` hold; the dojo scores it as `method: llm` against the
  clerk gold (`score_intake`).
- **Prepare** — a section map (heading + role + document-absolute char
  offsets, deterministically validated: in-bounds, monotonic, catalog roles)
  so downstream routing can see document structure.

**No-truncation doctrine (human directive 2026-09-03).** Documents are NEVER
truncated. Anything past an input budget is processed in overlapping sliding
windows (`agents.intake.sliding_windows` — paragraph-boundary, 15% overlap,
mirroring the extraction chunker) and merged deterministically: per-window
triage reads vote (plurality among non-unknown classes, ties on confidence),
section offsets are translated to document-absolute positions and
overlap-deduped, and partial-window cleaning is never spliced back. The same
doctrine governs the sorter: `agents/sorter.py` bypasses the vendored
HEAD+TAIL truncation — over-budget documents are classified window-by-window
and the reads merge (plurality vote, mean confidence of the agreeing windows,
first non-null subtype, joined reasoning; `WINDOW i OF n` markers on every
call).

**Gate + cost.** The LLM pass fires ONLY for documents that need it
(`looks_messy`, or longer than the sorter's input budget — clean short
documents pay zero). One fused call per window on the cheapest paid model
(`qwen3.7-flash`; the free tier stays the Gmail triage lane's privilege).
`MAILROOM_LLM_INTAKE=0` disables the LLM pass entirely; every failure fails
soft to the deterministic clerk output — intake never blocks a run.

---

### 9. Boss (`agents/boss.py`)

| Attribute | Value |
|---|---|
| **Node** | `boss_escalation` |
| **Trigger** | Data conflict or repeated low confidence |
| **Input** | Manifest + conflicting matter context |
| **Output** | Decision: approved or review |
| **Personality** | Calm under pressure, makes the judgment call |

**Two implementation paths, one personality:**

1. **In-graph (`boss_escalation` node)**: synchronously adjudicates within a document's pipeline run.
2. **Ops-monitor (`pipeline/ops_monitor.py`)**: separate scheduled process sweeping the catalog for systemic issues.

Both share the same system prompt voice — consistent persona across both invocation contexts.

---

### 10. PDF Transcriber (`agents/pdf_transcriber.py`)

| Attribute | Value |
|---|---|
| **Node** | `intake` (via `_read_file_text`) |
| **Trigger** | PDF with < `pdf_direct_chars_per_page` chars/page (scanned/garbled) |
| **Input** | PDF file |
| **Output** | Markdown text + confidence + method (`direct` / `llm`) |
| **Personality** | Faithful transcription only — no fact changes |

A hybrid agent: text-based PDFs are transcribed **directly** from `pdfplumber`/`pypdf` extraction (no LLM, seconds), while scanned or garbled PDFs get an LLM markdown reformat pass. The threshold is `pipeline.pdf_direct_chars_per_page` in `taxonomy.yaml`.

---

### 10b. Image Extractor (`agents/image_extractor.py`)

| Attribute | Value |
|---|---|
| **Node** | `intake` (via `_read_file_text` / `_extract_text_from_image`) |
| **Trigger** | Image file (jpg/png/gif/webp/tiff/bmp) |
| **Input** | Image bytes as a data-URI |
| **Output** | Visible text + confidence + method (`vision`) |
| **Personality** | Faithful transcription of visible text — no interpretation |

A vision LLM agent with its own `taxonomy.yaml` entry and Langfuse-managed prompt (`mailroom-image_extractor`). Transient provider failures retry through `retry_chat_completion`; persistent failure raises so the intake node can route the document to review rather than silently substituting a fallback marker.

---

### 11. Judge (`agents/judge.py`)

| Attribute | Value |
|---|---|
| **Node** | `judge_verify` (Lane B) **and** offline (`scripts/run_quality_judges.py`) |
| **Trigger** | In-graph: any extraction in the ambiguous band (`low <= extraction_confidence < judge_band_high`). Offline: a finished pilot run. |
| **Input** | Document text + extracted data (+ sorter reasoning, offline) |
| **Output** | Completeness / classification / correctness scores + labels |
| **Personality** | Expert legal reviewer; rubric-driven, evidence-citing |

The Judge is **part of the document graph** as the gated Lane B completeness check (`judge_verify_node`). Most documents never see this call: clean high-confidence extractions skip it (zero added LLM calls). When it fires, verdicts land on state (`complete` proceeds, `partial`/`incomplete` go to the arbiter, hard failure fail-safes) and scores are emitted via `observability.scores.emit_in_pipeline_judge_scores`.

The same agent also runs **offline** against a finished pilot (`scripts/run_quality_judges.py`) and audits pipeline output against the **task specification** (taxonomy doc classes + extraction schemas):

| Method | Measures |
|---|---|
| `judge_completeness` | Did the specialist capture every field the document states? |
| `judge_classification` | Is the sorter's assigned class correct for the document? |
| `judge_extraction_correctness` | Are extracted values factually accurate (no fabrication)? |

Each dimension returns a score + label + reasoning, ingested as Langfuse scores on the document's trace. Offline run: `PYTHONPATH=src python src/scripts/run_quality_judges.py --real` (or `--mock`).

---

### 11b. Arbiter (`agents/arbiter.py`)

| Attribute | Value |
|---|---|
| **Node** | `arbiter` (Lane B) |
| **Trigger** | In-pipeline judge verdict is `partial` or `incomplete` |
| **Input** | Specialist extraction **and** the judge's findings |
| **Output** | Bounded decision: `accept_with_caveats` / `retry_extraction` / `human_review` |
| **Personality** | Final judgment authority; constrained to three outcomes |

When the completeness judge rejects an extraction, the arbiter — not the raw
pipeline — decides. `retry_extraction` is bounded by `arbiter_retry_max` (`retry_extraction`
approval-inclusive; the state counter `arbiter_retry_count` is compared against it); exhausted retries escalate to human review.

---

### 12. Gmail intake triage (`agents/gmail_triage.py`)

| Attribute | Value |
|---|---|
| **Node** | none — the single-document Gmail triage lane (watcher claim time, Gmail channel only) |
| **Trigger** | one accepted attachment per email (`route: triage`) and `MAILROOM_GMAIL_TRIAGE` on (default with the channel) |
| **Input** | The same `doc_text` the pipeline would read (via `graph.build_graph._read_file_text`, after deterministic `apply_intake` prep) |
| **Output** | `intake.triage`: `primary_doc_class` + `doc_subclass` + `confidence` + `gist` + `keywords`; own `triage_*` audit section |
| **Personality** | Fast, grounded intake clerk — the accurate log, not the final word |

The **free OpenRouter triage team** (`z-ai/glm-5.2:free`, $0 in `cost_models`,
rate-limited) — the free model is deliberate: single-document Gmail uploads
must not rack up paid-agent spend. The lane performs the **core steps and
functionalities of the full pipeline** — deterministic preparation, triage
classification, auditable-hash archive with a terminal manifest, and the
completion echo — without calling any paid agent. Emails with **two or more
accepted attachments drop the triage approach** and run the FULL paid
pipeline per document (`route: pipeline`; triage is never dispatched).

> The end-to-end operator manual for the Gmail intake route — enabling the
> channel, the upload/subject-line format contract, all pathways from Gmail
> into the pipeline, and troubleshooting — is
> [`docs/gmail-intake.md`](gmail-intake.md).

**Capability pre-check + honest handoff.** Before the lane runs, a
deterministic, LLM-free check (`pipeline/watcher.py:_triage_capability_check`)
verifies the free team can actually handle the single document — no doomed
runs. Documents beyond the free models' reach are handed off to the full paid
pipeline: image-only inputs (`image_requires_vision`), scanned PDFs with no
direct text (`scanned_pdf_requires_transcription`), unreadable inputs, or a
deterministic text length above the `gmail_triage` `max_input_chars` budget
(`exceeds_free_budget:N>M`) — **merger agreements are typically excessively
long and almost always exceed the free models' classification capability**.
The handoff reason rides `intake.triage_handoff` onto the terminal manifest
and the completion echo ("triage handoff: … — handled by the full pipeline").
Every canonical doc type — contract, merger_agreement, insurance_claim,
corporate_record, correspondence — is validated through the lane (test
matrix) and accepted when within the free capability envelope.

The triage read is **advisory by design** and never overrules the pipeline
agents (it is only dispatched on single-document Gmail instances, where no
pipeline run happens — the overrule guard is the standing invariant).
Audit entries use their own namespaced section (`triage_ingested` /
`triage_classified` / `triage_archived`) so the stored audits are never
conflated with the pipeline's `ingested`/`classified`/`extracted`/`archived`
vocabulary. Fails soft: no `OPENROUTER_API_KEY`, rate limit, or provider
error ever blocks intake (logged; the document parks to `failed/`). Output
is clamped to the live taxonomy vocabulary by `validate_triage` (unknown
class → `unknown`, confidence 0.0–1.0, ≤6 keywords, 300-char gist).
Registration: `llm/prompts.py:prompt_templates()` (synced with
`scripts/sync_prompts.py`), agent config in `config/taxonomy.yaml`.

---

### 13. Relations agent (`agents/relations.py`)

| Attribute | Value |
|---|---|
| **Node** | none — the post-archive association pass + the background archive sweep (HUB-040) |
| **Trigger** | deterministic layer: every terminal manifest (dispatched off the document path) + a watermark-incremental sweep every `MAILROOM_RELATIONS_SCAN_SECONDS` (embedded in the watcher). LLM judgment pass: config-gated (`relations.llm`, **OFF in the pilot**) |
| **Input** | deterministic: catalog + manifests + archived text (embeddings cached per document). LLM pass: top-k candidate pairs with signal evidence (gists/keywords — never raw text) |
| **Output** | typed, scored edges (`relation_edges`) + hash-chained ledger entries (`relation_log`) + advisory RELATED context for agents/echo + knowledge-graph exports |
| **Personality** | the mailroom's research clerk — files everything near everything it relates to, records the relationship itself |

The **relations layer** links associated topics, documents, and matters
across the archive — the lawyer's research methodology as infrastructure.
Deterministic signals (all free): `same_matter`, keyword Jaccard
(`topic_overlap`), shared parties (`party_overlap`), embedding cosine via
the dojo's sentence-transformers model (`semantic_similarity` — embeddings
computed ONCE per document and cached in `relation_embeddings`), and a
temporal evidence boost. Edges live in `relation_edges` (canonical
endpoints, closed six-type vocabulary, per-document cap); every scan and
every new edge is an entry in the **own hash-chained ledger** (`relation_log`,
`__relations__` scope — same tamper-evident law as the document audit;
`python -m pipeline.relations_scan --verify-ledger`), and each document's
own audit chain gains a `relations_linked` event.

The **LLM judgment pass** (`RelationsAgent.judge`) reviews the scanner's
top ambiguous candidates and returns typed judgments + rationale —
validated and clamped to the closed vocabulary; unproposed pairs and
invented types are refused, so nothing unvalidated ever reaches the ledger.
`relations.llm: false` keeps the pilot deterministic-only (free-tier
guardrail compatible); flipping it on in production with a paid model is a
taxonomy edit. Registered as `mailroom-relations` in `llm/prompts.py`.

**Consumption** (the longitudinal loop): a bounded, labeled advisory
`RELATED` block rides the sorter/specialist handoff context and the Gmail
completion echo — later documents inherit everything the archive already
knows. **Knowledge graphs** (`python -m pipeline.relations_graph`): matter
graphs (typed doc nodes + related-matter bridges), the global inter-matter
graph (edges aggregated to pair weights), and document ego-graphs, exported
as GraphJSON + GraphML (stdlib, always) and Plotly HTML + PNG (optional
deps, graceful skip) under `<base>/relations/graphs/`, with
`relations_graph_rendered` ledger events. Fails soft everywhere; the
document path never waits on it.

---

## Evaluating individual agents

Live Langfuse evaluators stay **pipeline-level** (`pipeline-result` generation, two independent judges) by design. To score one agent without running the 13-node graph, use the local isolation harness:

```bash
PYTHONPATH=src python src/scripts/run_agent_eval.py --list
PYTHONPATH=src python src/scripts/run_agent_eval.py --agent sorter --mock
PYTHONPATH=src python src/scripts/run_agent_eval.py --agent insurance_claims_specialist --mock --n 3
PYTHONPATH=src python src/scripts/run_agent_eval.py --agent all --mock --n 1 --self-check
```

`observability/agent_eval.py` loads labeled cases from test fixtures, local eval packs, and the live manifest; invokes a single agent; and scores with the same deterministic classifiers / field scorers the pipeline uses. `--real` is gated by `prepare_samples.is_real_sample` (CUAD / LegalBench only) — synthetic insurance / compliance / corporate / correspondence samples are mock-only, matching `run_pilot.py`.

This is the methodology for iterating on a single specialist or the sorter without paying for a full document-pipeline run. It does **not** replace the live `mailroom-pipeline-judge` / `mailroom-pipeline-quality` evaluators.

---

## Adding a New Agent

1. Define the extraction schema in `schemas/documents.py`:
   ```python
   class NewDocTypeExtraction(BaseModel):
       field_1: str = ""
       field_2: str | None = None
   ```

2. Register the schema in `EXTRACTION_SCHEMAS` dict.

3. Create the agent in `agents/` with its prompt as a module-level template constant:
   ```python
   SYSTEM_PROMPT = """..."""
   class NewDocTypeSpecialist(BaseAgent):
       agent_name = "new_specialist"
       def system_prompt(self) -> str:
           text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
           return text
       def extract(self, doc_text: str) -> dict: ...
   ```

4. Add a dispatch entry in `graph/build_graph.py` under `extract_node` and `retry_extract_node`.

5. Add the agent config in `config/taxonomy.yaml` under both `doc_classes` and `agents` (with `max_tokens`).

6. Register the template in `llm/prompts.py:prompt_templates()` and sync:
   `PYTHONPATH=src python src/scripts/sync_prompts.py`
