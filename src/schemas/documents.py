import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

_MONEY_RE = re.compile(r"^\(?-?\$?\s*-?[\d,]*\.?\d+\)?$")


def _money(value: Any) -> Any:
    """Accept the currency phrase the prompts allow ("$18,530.00", "1,250").

    Prompts say "amount as stated", and paths without the numeric
    JSON-schema grammar can still send a string. A float-only field rejected
    every "$"/comma amount (SAND-042 RCA).
    Unparseable prose is left as-is so validation still fails loudly.
    """
    if isinstance(value, str):
        text = value.strip().replace("USD", "").strip()
        if not text:
            return None
        if _MONEY_RE.match(text):
            negative = text.startswith("(") or "-" in text
            digits = re.sub(r"[^\d.]", "", text)
            return -float(digits) if negative else float(digits)
    return value


class ContractExtraction(BaseModel):
    # Pared CUAD product: key entities + the fixed 41-category checklist.
    # Open-ended key_obligations / termination_clauses are no longer extracted.
    # MAUD merger agreements use MergerAgreementExtraction — these leftover
    # merger_* / maud_* keys stay nullable so older CUAD payloads still parse.
    document_name: str | None = None
    parties: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    term_length: str | None = None
    governing_law: str | None = None
    contract_value: str | None = None
    renewal_terms: str | None = None
    cuad_family: str | None = None
    merger_consideration: str | None = None
    # Present CUAD / answered MAUD categories as "<label>: <evidence>" lines.
    cuad_clauses: list[str] = Field(default_factory=list)
    maud_clauses: list[str] = Field(default_factory=list)
    # Per-field reasoning trace (v24+ vendored schema): how each value was
    # found. A TRACE artifact, not clause content — excluded from scoring,
    # judge input, and the client-facing report.
    reasoning: dict | None = None


class MergerAgreementExtraction(BaseModel):
    # Dedicated MAUD schema (v0). CUAD family/clauses are not primary here.
    document_name: str | None = None
    parties: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    effective_time: str | None = None
    governing_law: str | None = None
    merger_consideration: str | None = None
    maud_clauses: list[str] = Field(default_factory=list)
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    reasoning: dict | None = None


class CorporateRecordExtraction(BaseModel):
    # Empty-field contract (dojo schema_contract): unstated scalar → null.
    entity_name: str | None = None
    record_type: str | None = None  # articles_of_incorporation, bylaws, powers_of_attorney, rights_instrument, other
    effective_date: str | None = None
    signatories: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    filing_number: str | None = None
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)


class CorrespondenceExtraction(BaseModel):
    sender: str | None = None
    recipient: str | None = None
    additional_recipients: list[str] = Field(default_factory=list)
    communication_type: str | None = None  # email, letter, memo, notice, demand, attorney_demand, press_release, meeting_request
    communication_date: str | None = None
    demand_amount: float | None = None
    action_items: list[str] = Field(default_factory=list)  # capped ≤3
    urgency: str | None = None  # routine, time-sensitive, urgent, critical
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _demand_money = field_validator("demand_amount", mode="before")(_money)


class InsuranceClaimExtraction(BaseModel):
    claim_number: str | None = None
    policy_number: str | None = None
    # Empty-field contract (dojo schema_contract): unstated scalar → null.
    # These five were `str = ""`, so a prompt-faithful null — and the
    # harness's own "" → None normalizer — failed validation: schema-valid
    # 26–34 % on SAND-042 (auto 0/81, carrier 0/30 over SAND-032).
    insurer: str | None = None
    insured_party: str | None = None
    claim_type: str | None = None  # pde, inpatient, outpatient, carrier, auto, property, liability, health, life, workers_comp, other
    date_of_loss: str | None = None
    date_filed: str | None = None
    claimed_amount: float | None = None
    # CMS / DE-SynPUF rows often have no named adjuster — null must validate
    # (production HF pilot parked REVIEW when this field was a required str).
    adjuster: str | None = None
    damages_description: str | None = None
    coverage_determination: str | None = None  # approved, denied, partial, pending
    denial_reasons: list[str] = Field(default_factory=list)
    supporting_documents: list[str] = Field(default_factory=list)
    intent: str | None = None
    subject_matter: str | None = None
    keywords: list[str] = Field(default_factory=list)
    # Fixed claim checklist as "<Category>: <answer/evidence>" (present only).
    claim_checklist: list[str] = Field(default_factory=list)
    confidence: float = 0.0

    _claimed_money = field_validator("claimed_amount", mode="before")(_money)


EXTRACTION_SCHEMAS: dict[str, type[BaseModel]] = {
    "contract": ContractExtraction,
    # MAUD merger agreements are a distinct live class with their own
    # specialist and schema — not an extract alias of CUAD contract.
    "merger_agreement": MergerAgreementExtraction,
    "corporate_record": CorporateRecordExtraction,
    "correspondence": CorrespondenceExtraction,
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
    except ImportError:
        pass
    return None
