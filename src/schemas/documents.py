from pydantic import BaseModel, Field


class ContractExtraction(BaseModel):
    # document_name matches the vendored LangChain contracts specialist schema
    # (CONTRACTS_SCHEMA) — normalize_extraction guarantees every field present.
    document_name: str | None = None
    parties: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    term_length: str | None = None
    termination_clauses: list[str] = Field(default_factory=list)
    governing_law: str | None = None
    key_obligations: list[str] = Field(default_factory=list)
    contract_value: str | None = None
    renewal_terms: str | None = None
    cuad_family: str | None = None
    merger_consideration: str | None = None
    cuad_clauses: list[str] = Field(default_factory=list)
    maud_clauses: list[str] = Field(default_factory=list)
    # Per-field reasoning trace (v24+ vendored schema): how each value was
    # found. A TRACE artifact, not clause content — excluded from scoring,
    # judge input, and the client-facing report.
    reasoning: dict | None = None


class CorporateRecordExtraction(BaseModel):
    entity_name: str = ""
    record_type: str = ""  # articles_of_incorporation, bylaws, powers_of_attorney, rights_instrument, other
    effective_date: str | None = None
    key_provisions: list[str] = Field(default_factory=list)
    signatories: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    filing_number: str | None = None


class CorrespondenceExtraction(BaseModel):
    sender: str = ""
    recipient: str = ""
    additional_recipients: list[str] = Field(default_factory=list)
    communication_type: str = ""  # email, letter, memo, notice, demand, attorney_demand, press_release, meeting_request
    communication_date: str | None = None
    key_points: list[str] = Field(default_factory=list)
    demand_amount: float | None = None
    action_items: list[str] = Field(default_factory=list)
    urgency: str = ""
    referenced_communications: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class ComplianceFilingExtraction(BaseModel):
    filing_type: str = ""  # 10-K, 10-Q, 8-K, S-1, DEF 14A, 13D, 13G, Form 4, 20-F, 6-K, other
    regulatory_body: str = ""
    filing_date: str | None = None
    due_date: str | None = None
    entity_name: str = ""
    key_requirements: list[str] = Field(default_factory=list)
    status: str | None = None
    reference_number: str | None = None


class InsuranceClaimExtraction(BaseModel):
    claim_number: str | None = None
    policy_number: str | None = None
    insurer: str = ""
    insured_party: str = ""
    claim_type: str = ""  # pde, inpatient, outpatient, carrier, auto, property, liability, health, life, workers_comp, other
    date_of_loss: str | None = None
    date_filed: str | None = None
    claimed_amount: float | None = None
    # CMS / DE-SynPUF rows often have no named adjuster — null must validate
    # (production HF pilot parked REVIEW when this field was a required str).
    adjuster: str | None = None
    damages_description: str = ""
    coverage_determination: str = ""  # approved, denied, partial, pending
    denial_reasons: list[str] = Field(default_factory=list)
    supporting_documents: list[str] = Field(default_factory=list)
    confidence: float = 0.0


EXTRACTION_SCHEMAS: dict[str, type[BaseModel]] = {
    "contract": ContractExtraction,
    # MAUD merger agreements share the CUAD field map (parties, dates,
    # maud_clauses, …) but are a distinct live class — not an extract alias.
    "merger_agreement": ContractExtraction,
    "corporate_record": CorporateRecordExtraction,
    "correspondence": CorrespondenceExtraction,
    "compliance_filing": ComplianceFilingExtraction,
    "insurance_claim": InsuranceClaimExtraction,
}


def get_extraction_schema(doc_type: str) -> type[BaseModel] | None:
    if doc_type in EXTRACTION_SCHEMAS:
        return EXTRACTION_SCHEMAS[doc_type]
    try:
        from pipeline.config import resolve_extract_class

        resolved = resolve_extract_class(doc_type)
        if resolved:
            return EXTRACTION_SCHEMAS.get(resolved)
    except Exception:
        pass
    return None
