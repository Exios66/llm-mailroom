from pydantic import BaseModel, Field


class ContractExtraction(BaseModel):
    parties: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    term_length: str | None = None
    termination_clauses: list[str] = Field(default_factory=list)
    governing_law: str | None = None
    key_obligations: list[str] = Field(default_factory=list)
    contract_value: str | None = None
    renewal_terms: str | None = None


class CorporateRecordExtraction(BaseModel):
    entity_name: str = ""
    record_type: str = ""
    effective_date: str | None = None
    key_provisions: list[str] = Field(default_factory=list)
    signatories: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    filing_number: str | None = None


class DueDiligenceExtraction(BaseModel):
    target_entity: str = ""
    diligence_type: str = ""
    material_findings: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    outstanding_items: list[str] = Field(default_factory=list)
    document_date: str | None = None
    prepared_by: str | None = None


class CorrespondenceExtraction(BaseModel):
    sender: str = ""
    recipient: str = ""
    date_sent: str | None = None
    subject: str = ""
    communication_type: str = ""
    key_points: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    urgency: str | None = None


class ComplianceFilingExtraction(BaseModel):
    filing_type: str = ""
    regulatory_body: str = ""
    filing_date: str | None = None
    due_date: str | None = None
    entity_name: str = ""
    key_requirements: list[str] = Field(default_factory=list)
    status: str | None = None
    reference_number: str | None = None


EXTRACTION_SCHEMAS: dict[str, type[BaseModel]] = {
    "contract": ContractExtraction,
    "corporate_record": CorporateRecordExtraction,
    "due_diligence": DueDiligenceExtraction,
    "correspondence": CorrespondenceExtraction,
    "compliance_filing": ComplianceFilingExtraction,
}


def get_extraction_schema(doc_type: str) -> type[BaseModel] | None:
    return EXTRACTION_SCHEMAS.get(doc_type)
