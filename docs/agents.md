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

    def _call_llm(self, user_message, response_format=None, temperature=None) -> str: ...

    def _call_structured(self, user_message, json_schema, temperature=0.1) -> dict: ...
```

Key design points:
- `self.client` and `self.model` are resolved from `config/taxonomy.yaml` → `llm/providers.py` → `llm/client.py`
- `_call_structured()` uses OpenAI's JSON schema mode for reliable structured output
- Every agent has a distinct system prompt ("personality") aligned with its role

---

## Agent Roster

### 1. Sorter (`agents/sorter.py`)

| Attribute | Value |
|---|---|
| **Node** | `classify`, `retry_classify` |
| **Trigger** | New document in `/processing` |
| **Input** | Raw document text |
| **Output** | `doc_type` + `confidence` + `reasoning` |
| **Personality** | Fast, decisive, flags ambiguity instead of guessing |

**System prompt seed:** "You are a fast, decisive legal document classifier operating in a transactional/corporate law firm's mailroom."

The Sorter is the first LLM call in the pipeline. It reads the document text and determines which of the configured document classes it belongs to. The list of available classes is dynamically read from `config/taxonomy.yaml`, so adding a new document type automatically expands the Sorter's options.

---

### 2. Contracts Specialist (`agents/contracts_specialist.py`)

| Attribute | Value |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == contract` |
| **Input** | Contract text + `ContractExtraction` schema |
| **Output** | Structured extraction + confidence |
| **Personality** | Meticulous, formal, precise to a fault |

**Output schema fields:**
| Field | Type | Description |
|---|---|---|
| `parties` | `list[str]` | All named parties |
| `effective_date` | `str \| None` | Contract effective date |
| `term_length` | `str \| None` | Duration |
| `termination_clauses` | `list[str]` | Termination provisions |
| `governing_law` | `str \| None` | Governing jurisdiction |
| `key_obligations` | `list[str]` | Performance obligations |
| `contract_value` | `str \| None` | Total value |
| `renewal_terms` | `str \| None` | Renewal conditions |

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
| `date_sent` | `str \| None` | When it was sent |
| `subject` | `str` | Topic |
| `communication_type` | `str` | letter, email, memo, notice, demand, etc. |
| `key_points` | `list[str]` | Main points made |
| `action_items` | `list[str]` | Actions required |
| `urgency` | `str \| None` | routine, time-sensitive, urgent, critical |

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

### 7. Reporter (`agents/reporter.py`)

| Attribute | Value |
|---|---|
| **Node** | `compile_report` |
| **Trigger** | Extraction complete, confidence sufficient |
| **Input** | All manifest data for the document |
| **Output** | Matter-record summary entry |
| **Personality** | Big-picture synthesizer, clean summaries |

The Reporter does NOT extract new data — it compiles and refines what the specialists already extracted. Its output goes into the `extracted_data._report` field.

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

## Adding a New Agent

1. Define the extraction schema in `schemas/documents.py`:
   ```python
   class NewDocTypeExtraction(BaseModel):
       field_1: str = ""
       field_2: str | None = None
   ```

2. Register the schema in `EXTRACTION_SCHEMAS` dict.

3. Create the agent in `agents/`:
   ```python
   class NewDocTypeSpecialist(BaseAgent):
       agent_name = "new_specialist"
       def system_prompt(self) -> str: ...
       def extract(self, doc_text: str) -> dict: ...
   ```

4. Add a dispatch entry in `graph/build_graph.py` under `extract_node` and `retry_extract_node`.

5. Add the agent config in `config/taxonomy.yaml` under both `doc_classes` and `agents`.
