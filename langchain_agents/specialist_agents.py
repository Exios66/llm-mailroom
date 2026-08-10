# VENDORED from github.com/Exios66/llm-entity-extraction (commit 055df31, ~v0.11.0).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""Specialist agents for field extraction from each document type (LangChain).

Each specialist knows how to extract fields specific to its document type and
is driven by a versioned prompt from ``src.prompts``. Schemas are exported as
module constants so the eval loops and judges can reference the same contracts.
"""

from __future__ import annotations

import structlog
from langchain_agents.base_agent import BaseAgent, build_structured_schema
from langchain_agents.prompts import get_prompt

logger = structlog.get_logger(__name__)


def _nullable_string(description: str = "") -> dict:
    return {"type": ["string", "null"], "description": description}


def _string_array(description: str = "") -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def normalize_extraction(result: dict, schema: dict) -> dict:
    """Guarantee the extraction carries EVERY schema field.

    The model occasionally omits a field (e.g. ``confidence``) or returns a
    malformed shape. This fills missing keys with their schema defaults
    (null for nullable strings, [] for arrays, 0.0 for numbers) so downstream
    scoring and reporting always see a complete, conformant extraction.
    """
    normalized = dict(result or {})
    for key, spec in (schema.get("properties") or {}).items():
        if key in normalized and normalized[key] not in (None, ""):
            continue
        type_spec = spec.get("type")
        if isinstance(type_spec, list):
            type_spec = next((t for t in type_spec if t != "null"), type_spec[0])
        if type_spec == "array":
            normalized[key] = normalized.get(key) or []
        elif type_spec == "number":
            normalized[key] = normalized.get(key) if isinstance(normalized.get(key), (int, float)) else 0.0
        else:
            normalized[key] = normalized.get(key) if normalized.get(key) not in (None, "") else None
    return normalized


# =============================================================================
# Extraction schemas (single source of truth for specialists + judges)
# =============================================================================

CONTRACTS_SCHEMA = build_structured_schema({
    "document_name": _nullable_string("The name of the contract (e.g. 'Web Hosting Agreement')"),
    "parties": _string_array("The names of the contracting parties"),
    "effective_date": _nullable_string("YYYY-MM-DD (ISO)"),
    "term_length": _nullable_string("The full duration or term of the agreement, including any riders"),
    "termination_clauses": _string_array("Conditions under which the agreement can be terminated (verbatim operative language)"),
    "governing_law": _nullable_string("The jurisdiction whose laws govern the agreement (governing-law sentence only)"),
    "key_obligations": _string_array("Major obligations of each party (verbatim operative language, one item per obligation)"),
    "contract_value": _nullable_string("The monetary value or consideration"),
    "renewal_terms": _nullable_string("Renewal, extension, or rollover terms (automatic or otherwise)"),
    "confidence": {
        "type": "number", "minimum": 0.0, "maximum": 1.0,
        "description": "Evidence-grounded extraction confidence (share of fields found, "
                        "lowered by uncertain values or truncation; never a fixed default)",
    },
})

CORPORATE_RECORDS_SCHEMA = build_structured_schema({
    "entity_name": _nullable_string(),
    "record_type": _nullable_string("bylaws, resolution, minutes, cap table, etc."),
    "effective_date": _nullable_string("mm/dd/yyyy"),
    "key_provisions": _string_array(),
    "signatories": _string_array(),
    "jurisdiction": _nullable_string(),
    "filing_number": _nullable_string(),
})

DUE_DILIGENCE_SCHEMA = build_structured_schema({
    "target_entity": _nullable_string(),
    "diligence_type": _nullable_string("legal, financial, operational, tax, etc."),
    "material_findings": _string_array(),
    "risk_flags": _string_array(),
    "outstanding_items": _string_array(),
    "document_date": _nullable_string("mm/dd/yyyy"),
    "prepared_by": _nullable_string(),
})

CORRESPONDENCE_SCHEMA = build_structured_schema({
    "sender": _nullable_string(),
    "recipient": _nullable_string(),
    "additional_recipients": _string_array(),
    "communication_type": _nullable_string("letter, email, memo, notice, demand, etc."),
    "communication_date": _nullable_string("mm/dd/yyyy"),
    "key_points": _string_array(),
    "demand_amount": _nullable_string(),
    "action_items": _string_array(),
    "urgency": _nullable_string("high, medium, low, immediate, etc."),
    "referenced_communications": _string_array(),
})

COMPLIANCE_FILING_SCHEMA = build_structured_schema({
    "filing_type": _nullable_string("10-K, 10-Q, 8-K, DEF 14A, Schedule 13D, etc."),
    "regulatory_body": _nullable_string("SEC, state secretary, etc."),
    "filing_date": _nullable_string("mm/dd/yyyy"),
    "due_date": _nullable_string("mm/dd/yyyy"),
    "entity_name": _nullable_string(),
    "key_requirements": _string_array(),
    "status": _nullable_string("filed, pending, late, etc."),
    "reference_number": _nullable_string(),
})

COURT_OPINIONS_SCHEMA = build_structured_schema({
    "case_name": _nullable_string("e.g., Smith v. Jones"),
    "court": _nullable_string(),
    "date_decided": _nullable_string("mm/dd/yyyy"),
    "docket_number": _nullable_string(),
    "opinion_type": _nullable_string("majority, dissenting, concurring, per curiam, order"),
    "parties": _string_array(),
    "holding": _nullable_string(),
    "legal_issues": _string_array(),
    "outcome": _nullable_string("affirmed, reversed, remanded, dismissed, etc."),
    "citations": _string_array(),
    "authored_by": _nullable_string(),
})

SPECIALIST_SCHEMAS = {
    "contract": CONTRACTS_SCHEMA,
    "corporate_record": CORPORATE_RECORDS_SCHEMA,
    "due_diligence": DUE_DILIGENCE_SCHEMA,
    "correspondence": CORRESPONDENCE_SCHEMA,
    "compliance_filing": COMPLIANCE_FILING_SCHEMA,
    "court_opinion": COURT_OPINIONS_SCHEMA,
}


def get_extraction_schema(doc_type: str) -> dict | None:
    """Return the extraction JSON schema for a doc type (None if unknown)."""
    return SPECIALIST_SCHEMAS.get(doc_type)


# =============================================================================
# Specialist agents
# =============================================================================


class _SpecialistBase(BaseAgent):
    """Shared extract() implementation over a per-class schema."""

    schema: dict
    handoff_context: str | None = None

    def extract(
        self, doc_text: str, pages: list[str] | None = None  # MAILROOM PATCH: pages
    ) -> dict:
        truncated = self.truncate_input(doc_text)
        # When the sorter hands this document off, its classification is
        # prefixed to the extraction call so the specialist extracts with the
        # expected clause set in mind (mailroom chained pipeline).
        user_message = f"Extract fields from this {self._doc_label} document:\n\n{truncated}"
        if self.handoff_context:
            user_message = (
                f"{self.handoff_context}\n\n"
                f"Extract fields from this {self._doc_label} document:\n\n{truncated}"
            )
        result = self._call_structured(
            user_message,
            json_schema=self.schema,
            temperature=0.1,
            pages=pages,  # MAILROOM PATCH
        )
        if result.get("_parse_error"):
            logger.error("specialist_parse_error", agent=self.agent_name)
            return {"_parse_error": True}
        if self._confidence_missing(result):
            # The model occasionally omits `confidence`; derive it from the
            # evidence in THIS document (the share of schema fields actually
            # found) — the rule the prompt itself states.
            result["confidence"] = round(self._evidence_confidence(result), 4)
        # Guarantee every schema field is present (null/[]/0.0 defaults).
        return normalize_extraction(result, self.schema)

    def _confidence_missing(self, result: dict) -> bool:
        value = result.get("confidence")
        return value is None or (isinstance(value, (int, float)) and value == 0.0)

    def _evidence_confidence(self, result: dict) -> float:
        """Share of schema fields actually found in the extraction (0.0-1.0).

        List fields count as found when non-empty; string fields when
        non-null. The confidence never exceeds what the extracted facts
        justify, mirroring the specialist prompts' evidence rule.
        """
        properties = (self.schema.get("properties") or {})
        total = 0
        found = 0
        for key, spec in properties.items():
            if key == "confidence":
                continue
            value = result.get(key)
            type_spec = spec.get("type")
            if isinstance(type_spec, list):
                type_spec = next((t for t in type_spec if t != "null"), type_spec[0])
            total += 1
            if type_spec == "array":
                found += 1 if value not in (None, [], "") else 0
            else:
                found += 1 if value not in (None, "") else 0
        return found / total if total else 0.0

    @property
    def _doc_label(self) -> str:
        return self.agent_name.replace("_specialist", "").replace("_", " ")


class ContractsSpecialist(_SpecialistBase):
    agent_name = "contracts_specialist"
    schema = CONTRACTS_SCHEMA

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 prompt_version: str = "contracts_specialist"):
        super().__init__(model=model, api_key=api_key)
        self.prompt_version = prompt_version

    def system_prompt(self) -> str:
        return get_prompt(self.prompt_version)


class CorporateRecordsSpecialist(_SpecialistBase):
    agent_name = "corporate_records_specialist"
    schema = CORPORATE_RECORDS_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("corporate_records_specialist")


class DueDiligenceSpecialist(_SpecialistBase):
    agent_name = "due_diligence_specialist"
    schema = DUE_DILIGENCE_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("due_diligence_specialist")


class CorrespondenceSpecialist(_SpecialistBase):
    agent_name = "correspondence_specialist"
    schema = CORRESPONDENCE_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("correspondence_specialist")


class ComplianceFilingSpecialist(_SpecialistBase):
    agent_name = "compliance_specialist"
    schema = COMPLIANCE_FILING_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("compliance_specialist")


class CourtOpinionsSpecialist(_SpecialistBase):
    agent_name = "court_opinions_specialist"
    schema = COURT_OPINIONS_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("court_opinions_specialist")


# Specialist registry — maps doc_type keys to specialist classes
SPECIALIST_REGISTRY = {
    "contract": ContractsSpecialist,
    "corporate_record": CorporateRecordsSpecialist,
    "due_diligence": DueDiligenceSpecialist,
    "correspondence": CorrespondenceSpecialist,
    "compliance_filing": ComplianceFilingSpecialist,
    "court_opinion": CourtOpinionsSpecialist,
}


def get_specialist(doc_type: str, model: str | None = None, api_key: str | None = None) -> BaseAgent:
    """Get the specialist agent for a given document type.

    Args:
        doc_type: Document type key (e.g., "contract").
        model: Optional model override.
        api_key: Optional API key override.

    Returns:
        An instantiated specialist agent.

    Raises:
        ValueError: If no specialist exists for the doc_type.
    """
    if doc_type not in SPECIALIST_REGISTRY:
        raise ValueError(f"No specialist registered for doc_type: {doc_type}")
    return SPECIALIST_REGISTRY[doc_type](model=model, api_key=api_key)
