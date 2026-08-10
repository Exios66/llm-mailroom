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

**Two of the agents — the Sorter and the Contracts Specialist — are vendored LangChain agents** (from `github.com/Exios66/llm-entity-extraction`, verified against commit `3a03d5c`, 2026-08-10 — issue #10 alignment check), imported into `langchain_agents/` with mailroom plumbing adapted in (pages/vision, run-deadline checks, per-call usage accounting — each adaptation marked `MAILROOM PATCH`). They use `langchain-openai`'s `ChatOpenAI` + `with_structured_output` instead of the mailroom's `agents/base.py` plumbing, and their system prompts are the eval-validated *versioned* prompts (`sorter_v5`, `contracts_specialist_v11`) from `langchain_agents/prompts.py` — they bypass `get_managed_prompt`/Langfuse prompt linking (generations are still auto-traced via the langfuse-openai SDK patch). All other agents follow the `BaseAgent` contract below.

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

The Sorter is a **vendored LangChain agent** (`agents/sorter.py` re-exports `langchain_agents.sorter_agent.SorterAgent`): it classifies via `with_structured_output` against the `SORTER_SCHEMA`, uses the eval-validated `sorter_v5` prompt, and adds a **contract-subtype dimension** — for contracts it assigns one of 25 CUAD agreement families (affiliate, license, distributor, franchise, …) plus `other` (`CONTRACT_SUBTYPE_KEYS`, normalized via `normalize_subtype`; non-contracts carry `contract_subtype=None`). `classify()` returns a 4-tuple `(doc_type, contract_subtype, confidence, reasoning)`; the subtype flows into state, the classification guard, the extraction handoff context, the report, and the catalog. Truncation past the input budget uses the upstream **HEAD+TAIL window** (opening + closing portions where term/termination/governing-law/signatures live).

---

### 2. Contracts Specialist (`agents/contracts_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == contract` |
| **Input** | Contract text + `ContractExtraction` schema (+ page images) |
| **Output** | Structured extraction + confidence |
| **Personality** | Meticulous, formal, precise to a fault |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `document_name` | `str \| None` | The name of the contract (e.g. 'Web Hosting Agreement') |
| `parties` | `list[str]` | All named parties |
| `effective_date` | `str \| None` | Contract effective date |
| `term_length` | `str \| None` | Duration |
| `termination_clauses` | `list[str]` | Termination provisions |
| `governing_law` | `str \| None` | Governing jurisdiction |
| `key_obligations` | `list[str]` | Performance obligations |
| `contract_value` | `str \| None` | Total value |
| `renewal_terms` | `str \| None` | Renewal conditions |

The Contracts Specialist is also a **vendored LangChain agent** (`agents/contracts_specialist.py` re-exports `langchain_agents.specialist_agents.ContractsSpecialist`): `contracts_specialist_v11` prompt, `normalize_extraction` guarantees every schema field is present, and a missing `confidence` is derived from the share of fields actually found. It accepts a **`handoff_context`** — the chained-eval pattern: the graph passes the sorter's classification (`doc_type` + `contract_subtype` + confidence) into the extraction call so the specialist extracts with the expected clause set of that agreement family in mind. The other five specialists accept the same optional `handoff_context` parameter.

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
| `record_type` | `str` | bylaws, resolution, minutes, formation, etc. |
| `effective_date` | `str \| None` | Date the record took effect |
| `key_provisions` | `list[str]` | Key governance provisions |
| `signatories` | `list[str]` | Who signed/approved |
| `jurisdiction` | `str \| None` | State/country of incorporation |
| `filing_number` | `str \| None` | Official filing reference |

---

### 4. Due Diligence Specialist (`agents/due_diligence_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == due_diligence` |
| **Input** | Document text + `DueDiligenceExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Skeptical, flags inconsistencies aggressively |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `target_entity` | `str` | Entity being investigated |
| `diligence_type` | `str` | Financial, legal, operational, etc. |
| `material_findings` | `list[str]` | Significant facts discovered |
| `risk_flags` | `list[str]` | Identified risks |
| `outstanding_items` | `list[str]` | Open questions |
| `document_date` | `str \| None` | Report date |
| `prepared_by` | `str \| None` | Author |

---

### 5. Correspondence Specialist (`agents/correspondence_specialist.py`)

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
| `key_points` | `list[str]` | Main points made |
| `demand_amount` | `float \| None` | Exact dollar amount demanded (demand letters) |
| `action_items` | `list[str]` | Actions required |
| `urgency` | `str` | routine, time-sensitive, urgent, critical |
| `referenced_communications` | `list[str]` | Prior letters/notices this message references |
| `confidence` | `float` | Extraction confidence (evidence-derived) |

---

### 6. Compliance Specialist (`agents/compliance_specialist.py`)

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

---

### 7. Court Opinions Specialist (`agents/court_opinions_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == court_opinion` |
| **Input** | Document text + `CourtOpinionExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Meticulous, reports holdings without editorializing |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `case_name` | `str` | Style of the case (e.g. Smith v. Jones) |
| `court` | `str` | Issuing court |
| `date_decided` | `str \| None` | Decision date |
| `docket_number` | `str \| None` | Case/docket number |
| `opinion_type` | `str` | published, memorandum, per curiam, order, etc. |
| `parties` | `list[str]` | Named parties from the caption |
| `holding` | `str` | The rule of law established |
| `legal_issues` | `list[str]` | Questions of law presented and decided |
| `outcome` | `str` | affirmed, reversed, remanded, denied, granted |
| `citations` | `list[str]` | Reporter citations and docket numbers |
| `authored_by` | `str \| None` | Authoring judge |

---

### 8. Reporter (`agents/reporter.py`)

| Attribute | Value |
|---|---|
| **Node** | `compile_report` |
| **Trigger** | Extraction complete, confidence sufficient |
| **Input** | All manifest data for the document |
| **Output** | Matter-record summary entry |
| **Personality** | Big-picture synthesizer, clean summaries |

The Reporter does NOT extract new data — it compiles and refines what the specialists already extracted. Its output goes into the `extracted_data._report` field.

---

### 9. Archivist (`agents/archivist.py`)

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

### 10. Boss (`agents/boss.py`)

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

### 11. PDF Transcriber (`agents/pdf_transcriber.py`)

| Attribute | Value |
|---|---|
| **Node** | `ingest` (via `_read_file_text`) |
| **Trigger** | PDF with < `pdf_direct_chars_per_page` chars/page (scanned/garbled) |
| **Input** | PDF file |
| **Output** | Markdown text + confidence + method (`direct` / `llm`) |
| **Personality** | Faithful transcription only — no fact changes |

A hybrid agent: text-based PDFs are transcribed **directly** from `pdfplumber`/`pypdf` extraction (no LLM, seconds), while scanned or garbled PDFs get an LLM markdown reformat pass. The threshold is `pipeline.pdf_direct_chars_per_page` in `taxonomy.yaml`.

---

### 12. Judge (`agents/judge.py`)

| Attribute | Value |
|---|---|
| **Node** | None — offline evaluator (`scripts/run_quality_judges.py`) |
| **Trigger** | Pilot run complete |
| **Input** | Document text + extracted data (+ sorter reasoning) |
| **Output** | Task-spec scores: completeness, classification, correctness |
| **Personality** | Expert legal reviewer; rubric-driven, evidence-citing |

The Judge is not part of the document graph. It audits pipeline output against the **task specification** (taxonomy doc classes + extraction schemas):

| Method | Measures |
|---|---|
| `judge_completeness` | Did the specialist capture every field the document states? |
| `judge_classification` | Is the sorter's assigned class correct for the document? |
| `judge_extraction_correctness` | Are extracted values factually accurate (no fabrication)? |

Each dimension returns a score + label + reasoning, ingested as Langfuse scores on the document's trace. Run with `python scripts/run_quality_judges.py --real` (or `--mock`).

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
   `python scripts/sync_prompts.py`
