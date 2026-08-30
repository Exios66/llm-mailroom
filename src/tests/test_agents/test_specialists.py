import pytest

from schemas.documents import EXTRACTION_SCHEMAS, InsuranceClaimExtraction


def test_insurance_claim_schema_registered_and_validates():
    assert EXTRACTION_SCHEMAS["insurance_claim"] is InsuranceClaimExtraction
    parsed = InsuranceClaimExtraction.model_validate(
        {
            "claim_number": "2026-CLM-041701",
            "policy_number": "HO-44-88391-A",
            "insurer": "Acme Insurance Company",
            "claim_type": "property",
            "claimed_amount": 18530.00,
            "coverage_determination": "denied",
            "denial_reasons": ["late notice"],
        }
    )
    assert parsed.claim_number == "2026-CLM-041701"
    assert parsed.denial_reasons == ["late notice"]
    assert parsed.supporting_documents == []


def test_specialist_dispatch_includes_insurance_claim():
    from graph.build_graph import _build_specialist_dispatch

    dispatch = _build_specialist_dispatch()
    assert "insurance_claim" in dispatch


def test_insurance_claims_specialist_constructs_and_builds_schema():
    from agents.insurance_claims_specialist import InsuranceClaimsSpecialist

    agent = InsuranceClaimsSpecialist()
    assert agent.agent_name == "insurance_claims_specialist"
    # system_prompt resolves through get_managed_prompt's local fallback
    prompt = agent.system_prompt()
    assert "insurance claim" in prompt.lower()
    assert "coverage determination" in prompt.lower()


def test_insurance_claims_specialist_parse_error_path(mock_openai_client):
    from agents.insurance_claims_specialist import InsuranceClaimsSpecialist

    mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
        "not-json-at-all"
    )
    agent = InsuranceClaimsSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    result = agent.extract("CLAIM NO. 123 ...")
    assert result.get("_parse_error") is True
    assert result.get("confidence") == 0.3


def test_insurance_claims_specialist_happy_path(mock_openai_client):
    import json

    from agents.insurance_claims_specialist import InsuranceClaimsSpecialist

    payload = {
        "claim_number": "2026-CLM-041701",
        "policy_number": "HO-44-88391-A",
        "insurer": "Acme Insurance Company",
        "insured_party": "Jack B",
        "claim_type": "property",
        "date_of_loss": None,
        "date_filed": "2026-03-21",
        "claimed_amount": 18530.00,
        "adjuster": "J. Featherstone",
        "damages_description": "hail damage to roof and detached garage",
        "coverage_determination": "pending",
        "denial_reasons": [],
        "supporting_documents": ["contractor estimate"],
        "intent": "coverage_pending",
        "subject_matter": "hail damage to roof and detached garage",
        "keywords": ["property", "hail", "pending"],
        "claim_checklist": ["Coverage Determination: pending"],
        "confidence": 0.82,
    }
    mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
        json.dumps(payload)
    )
    agent = InsuranceClaimsSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    result = agent.extract("FNOL form text ...")
    for key, value in payload.items():
        assert result.get(key) == value, key
    assert result.get("confidence") == 0.82


class TestContractsSpecialist:
    def test_extract_contract(self, sample_contract_text, mock_langchain_llm):
        mock_langchain_llm.extraction = {
            "document_name": "Master Services Agreement",
            "parties": ["ACME Corporation", "Zenith Technologies LLC"],
            "effective_date": "2024-01-15",
            "term_length": "3 years",
            "cuad_clauses": [
                "Termination For Convenience: 60-day convenience",
                "Anti-Assignment: insolvency",
            ],
            "governing_law": "Delaware",
            "contract_value": "$2,500,000",
            "renewal_terms": "Automatic 1-year renewal",
            "confidence": 0.93,
        }
        from agents.contracts_specialist import ContractsSpecialist

        agent = ContractsSpecialist()
        result = agent.extract(sample_contract_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert "ACME" in str(result.get("parties", []))
        # normalize_extraction guarantees every schema field is present.
        assert "document_name" in result

    def test_extract_returns_confidence(self, sample_contract_text, mock_langchain_llm):
        mock_langchain_llm.extraction = {
            "parties": [],
            "effective_date": None,
            "term_length": None,
            "cuad_clauses": [],
            "governing_law": None,
            "contract_value": None,
            "renewal_terms": None,
            "confidence": 0.30,
        }
        from agents.contracts_specialist import ContractsSpecialist

        agent = ContractsSpecialist()
        result = agent.extract("vague document text")
        assert "confidence" in result
        assert isinstance(result["confidence"], (int, float))

    def test_extract_handoff_context_prefixed(
        self, sample_contract_text, mock_langchain_llm
    ):
        # Chained-eval pattern: the sorter's classification (incl. contract
        # subtype) is prefixed to the extraction call.
        calls = []
        mock_langchain_llm.on_call = lambda text, parsed: calls.append(text)
        from agents.contracts_specialist import ContractsSpecialist

        agent = ContractsSpecialist(
            handoff_context=(
                "Sorter classification: doc_type=contract contract_subtype=license. "
                "Extract this contract's fields accordingly, ensuring every clause "
                "of this agreement family is captured."
            )
        )
        agent.extract(sample_contract_text[:1000])
        assert calls
        assert "contract_subtype=license" in calls[0]
        assert "Extract fields from this contracts document" in calls[0]

    def test_extract_with_pages_builds_multimodal(
        self, sample_contract_text, mock_langchain_llm
    ):
        # Page data-URIs must be threaded into the multimodal human message.
        pages = ["data:image/png;base64,AAAA"]
        seen = {}

        def capture(text, parsed):
            seen["text"] = text

        mock_langchain_llm.on_call = capture
        from agents.contracts_specialist import ContractsSpecialist

        agent = ContractsSpecialist()
        result = agent.extract(sample_contract_text[:1000], pages=pages)
        assert seen.get("text", "").startswith("Extract fields from this contracts document")


class TestCorporateRecordsSpecialist:
    def test_extract_bylaws(self, sample_corporate_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"entity_name": "Meridian Holdings, Inc.", "record_type": "bylaws", '
            '"effective_date": "2023-02-01", '
            '"intent": "record_governance", '
            '"subject_matter": "Amended bylaws for Meridian Holdings", '
            '"keywords": ["annual meeting", "board size", "Delaware"], '
            '"signatories": ["Thomas Meridian", "Elizabeth Warren"], '
            '"jurisdiction": "Delaware", "filing_number": "DE-2023-884721", "confidence": 0.94}'
        )
        from agents.corporate_records_specialist import CorporateRecordsSpecialist
        agent = CorporateRecordsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract(sample_corporate_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert "Meridian" in result.get("entity_name", "")


class TestCorrespondenceSpecialist:
    def test_extract_demand_letter(self, sample_correspondence_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"sender": "Morrison & Chase LLP", "recipient": "Richard Palmer, NovaTech Solutions", '
            '"additional_recipients": [], '
            '"communication_date": "2024-06-12", '
            '"communication_type": "demand letter", '
            '"intent": "demand_payment", '
            '"subject_matter": "Infringement of U.S. Patent 10,234,567 on OptiChip", '
            '"keywords": ["patent", "OptiChip", "cease and desist"], '
            '"demand_amount": 250000.0, '
            '"action_items": ["Cease and desist", "Provide accounting", "Enter negotiations within 14 days"], '
            '"urgency": "critical", "confidence": 0.96}'
        )
        from agents.correspondence_specialist import CorrespondenceSpecialist
        agent = CorrespondenceSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract(sample_correspondence_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert len(result.get("action_items", [])) > 0


class TestComplianceSpecialist:
    def test_extract_10k(self, sample_compliance_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"filing_type": "10-K Annual Report", "regulatory_body": "SEC", '
            '"filing_date": "2024-03-15", "due_date": null, '
            '"entity_name": "NovaTech Solutions, Inc.", '
            '"key_requirements": ["Annual report per Exchange Act Section 13 or 15(d)"], '
            '"status": "filed", "reference_number": "001-98765", "confidence": 0.95}'
        )
        from agents.compliance_specialist import ComplianceSpecialist
        agent = ComplianceSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract(sample_compliance_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert "10-K" in result.get("filing_type", "")


class TestBossAgent:
    def test_adjudicate_conflict(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"decision": "approved", "reasoning": "Conflict resolved", "resolution_notes": "Proceeding"}'
        )
        from agents.boss import BossAgent
        agent = BossAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.adjudicate(
            {"doc_id": "test-1", "doc_type": "contract", "extraction_confidence": 0.5},
            [{"doc_type": "contract", "extracted_data": {"parties": ["A", "B"]}}],
        )
        assert result.get("decision") in ("approved", "review")

    def test_analyze_system_metrics(self, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"assessment": "All clear", "severity": "info", '
            '"recommended_action": "none", "findings": ["No issues detected"]}'
        )
        from agents.boss import BossAgent
        agent = BossAgent()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.analyze_system_metrics({"stuck_docs": 0, "error_rate": 0.02})
        assert result.get("recommended_action") in ("none", "alert", "pause_ingestion")
