# Agents

Mailroom has 9 specialist agents. Each has a distinct system prompt ("personality") aligned with its role. All agents inherit from `agents/base.py:BaseAgent` and share a common structured-output interface.

## Agent Architecture

```python
class BaseAgent(ABC):
    agent_name: str    # Must match key in config/taxonomy.yaml agents:

    def __init__(self):
        self.client, self.model = get_llm(self.agent_name)

    def system_prompt(self) -> str: ...

    def _call_llm(self, user_message, response_format=None, temperature=None) -> str:
        """Raw LLM call with provider-agnostic client."""

    def _call_structured(self, user_message, json_schema, temperature=0.1) -> dict:
        """LLM call with OpenAI JSON schema mode for reliable structured output."""
```

Provider and model resolution: `agent_name` → `config/taxonomy.yaml` → `llm/client.py` → `llm/providers.py`. No agent code references a specific provider.

---

## Agent Roster

### 1. Sorter (`agents/sorter.py`)

| | |
|---|---|
| **Node** | `classify`, `retry_classify` |
| **Trigger** | New document in processing |
| **Output** | `doc_type` + `confidence` + `reasoning` |
| **Personality** | Fast, decisive, flags ambiguity |

The Sorter is the first LLM call. It categorizes documents into one of the configured classes (contract, corporate_record, due_diligence, correspondence, compliance_filing). Available classes are dynamically read from `config/taxonomy.yaml`.

---

### 2. Contracts Specialist (`agents/contracts_specialist.py`)

| | |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == contract` |
| **Personality** | Meticulous, formal, precise to a fault |

Extracts: parties, effective_date, term_length, termination_clauses, governing_law, key_obligations, contract_value, renewal_terms.

---

### 3. Corporate Records Specialist (`agents/corporate_records_specialist.py`)

| | |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == corporate_record` |
| **Personality** | Methodical, loves structure and hierarchy |

Extracts: entity_name, record_type, effective_date, key_provisions, signatories, jurisdiction, filing_number.

---

### 4. Due Diligence Specialist (`agents/due_diligence_specialist.py`)

| | |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == due_diligence` |
| **Personality** | Skeptical, flags inconsistencies aggressively |

Extracts: target_entity, diligence_type, material_findings, risk_flags, outstanding_items, document_date, prepared_by.

---

### 5. Correspondence Specialist (`agents/correspondence_specialist.py`)

| | |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == correspondence` |
| **Personality** | Reads between the lines, tracks narrative/intent |

Extracts: sender, recipient, date_sent, subject, communication_type, key_points, action_items, urgency.

---

### 6. Compliance Specialist (`agents/compliance_specialist.py`)

| | |
|---|---|
| **Node** | `extract`, `retry_extract` |
| **Trigger** | `doc_type == compliance_filing` |
| **Personality** | Rule-bound, cites authority, cautious |

Extracts: filing_type, regulatory_body, filing_date, due_date, entity_name, key_requirements, status, reference_number.

---

### 7. Reporter (`agents/reporter.py`)

| | |
|---|---|
| **Node** | `compile_report` |
| **Trigger** | Extraction complete, confidence sufficient |
| **Personality** | Big-picture synthesizer, clean summaries |

Does NOT extract new data — compiles and refines what specialists already extracted. Produces a matter-record entry suitable for client-facing records.

---

### 8. Archivist (`agents/archivist.py`)

| | |
|---|---|
| **Node** | `archive` |
| **Trigger** | Report compiled |
| **Personality** | Quiet, exhaustive, never skips a step |

**Not an LLM agent** — a procedural function that:
1. Moves file to `/archive/<matter_id>/<doc_type>/`
2. Writes manifest JSON sidecar
3. Creates hash-chained audit log entry

---

### 9. Boss (`agents/boss.py`)

| | |
|---|---|
| **Node** | `boss_escalation` |
| **Trigger** | Data conflict or repeated low confidence |
| **Personality** | Calm under pressure, makes the judgment call |

**Two implementation paths, one personality:**
1. **In-graph**: synchronously adjudicates within a document's pipeline run
2. **Ops-monitor**: separate scheduled process sweeping the catalog for systemic issues

---

## Agent Dispatch

Specialist dispatch in `graph/build_graph.py`:

```python
specialists = {
    "contract": _extract_contracts,
    "corporate_record": _extract_corporate_records,
    "due_diligence": _extract_due_diligence,
    "correspondence": _extract_correspondence,
    "compliance_filing": _extract_compliance,
}
extractor = specialists.get(doc_type, fallback)
```

## Adding a New Agent

1. Define extraction schema in `schemas/documents.py`
2. Register in `EXTRACTION_SCHEMAS` dict
3. Create agent class in `agents/`
4. Add dispatch entry in `graph/build_graph.py`
5. Add agent config in `config/taxonomy.yaml` under `doc_classes` and `agents`
