import pytest

from schemas.documents import EXTRACTION_SCHEMAS, CourtOpinionExtraction


def test_court_opinion_schema_registered_and_validates():
    assert EXTRACTION_SCHEMAS["court_opinion"] is CourtOpinionExtraction
    parsed = CourtOpinionExtraction.model_validate(
        {
            "case_name": "People v. Carter",
            "court": "Appellate Division",
            "holding": "affirmed",
            "legal_issues": ["search and seizure"],
            "outcome": "affirmed",
        }
    )
    assert parsed.case_name == "People v. Carter"
    assert parsed.parties == []


def test_specialist_dispatch_includes_court_opinion():
    from graph.build_graph import _build_specialist_dispatch

    dispatch = _build_specialist_dispatch()
    assert "court_opinion" in dispatch


class TestContractsSpecialist:
    def test_extract_contract(self, sample_contract_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"parties": ["ACME Corporation", "Zenith Technologies LLC"], '
            '"effective_date": "2024-01-15", "term_length": "3 years", '
            '"termination_clauses": ["30-day material breach", "60-day convenience", "insolvency"], '
            '"governing_law": "Delaware", "key_obligations": ["monthly status reports", "99.9% uptime"], '
            '"contract_value": "$2,500,000", "renewal_terms": "Automatic 1-year renewal", '
            '"confidence": 0.93}'
        )
        from agents.contracts_specialist import ContractsSpecialist
        agent = ContractsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract(sample_contract_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert "ACME" in str(result.get("parties", []))

    def test_extract_returns_confidence(self, sample_contract_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"parties": [], "effective_date": null, "term_length": null, '
            '"termination_clauses": [], "governing_law": null, "key_obligations": [], '
            '"contract_value": null, "renewal_terms": null, "confidence": 0.30}'
        )
        from agents.contracts_specialist import ContractsSpecialist
        agent = ContractsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract("vague document text")
        assert "confidence" in result
        assert isinstance(result["confidence"], (int, float))


class TestCorporateRecordsSpecialist:
    def test_extract_bylaws(self, sample_corporate_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"entity_name": "Meridian Holdings, Inc.", "record_type": "bylaws", '
            '"effective_date": "2023-02-01", '
            '"key_provisions": ["Annual meeting on 2nd Tuesday of May", "Board size 3-9"], '
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


class TestDueDiligenceSpecialist:
    def test_extract_dd_report(self, sample_dd_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"target_entity": "NovaTech Solutions, Inc.", '
            '"diligence_type": "Legal and Regulatory", '
            '"material_findings": ["12 issued patents", "Patent litigation in E.D. Texas"], '
            '"risk_flags": ["Patent litigation", "Customer concentration", "Employee attrition"], '
            '"outstanding_items": ["Litigation hold documentation", "Customer contract review"], '
            '"document_date": "2024-05-22", "prepared_by": "Morrison & Chase LLP", '
            '"confidence": 0.91}'
        )
        from agents.due_diligence_specialist import DueDiligenceSpecialist
        agent = DueDiligenceSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract(sample_dd_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert len(result.get("risk_flags", [])) > 0


class TestCorrespondenceSpecialist:
    def test_extract_demand_letter(self, sample_correspondence_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"sender": "Morrison & Chase LLP", "recipient": "Richard Palmer, NovaTech Solutions", '
            '"additional_recipients": [], '
            '"communication_date": "2024-06-12", '
            '"communication_type": "demand letter", '
            '"key_points": ["Infringement of U.S. Patent 10,234,567", "OptiChip product line"], '
            '"demand_amount": 250000.0, '
            '"action_items": ["Cease and desist", "Provide accounting", "Enter negotiations within 14 days"], '
            '"urgency": "critical", "referenced_communications": [], "confidence": 0.96}'
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


class TestCourtOpinionsSpecialist:
    def test_extract_opinion(self, sample_court_opinion_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"case_name": "People v. Carter", '
            '"court": "New York Supreme Court, Appellate Division", '
            '"date_decided": "2024-05-01", "docket_number": "2024-1847", '
            '"opinion_type": "per curiam", '
            '"parties": ["People of the State of New York", "John D. Carter"], '
            '"holding": "Automobile exception permits warrantless search of containers", '
            '"legal_issues": ["Suppression of warrantless search", "Weight of the evidence"], '
            '"outcome": "affirmed", "citations": ["United States v. Ross, 456 U.S. 798 (1982)"], '
            '"authored_by": null, "confidence": 0.95}'
        )
        from agents.court_opinions_specialist import CourtOpinionsSpecialist
        agent = CourtOpinionsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract(sample_court_opinion_text[:1000])
        assert result.get("confidence", 0) >= 0.80
        assert "People v. Carter" in result.get("case_name", "")
        assert result.get("outcome") == "affirmed"

    def test_extract_returns_confidence(self, sample_court_opinion_text, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"case_name": "", "court": "", "date_decided": null, '
            '"docket_number": null, "opinion_type": "", "parties": [], '
            '"holding": "", "legal_issues": [], "outcome": "", "citations": [], '
            '"authored_by": null, "confidence": 0.30}'
        )
        from agents.court_opinions_specialist import CourtOpinionsSpecialist
        agent = CourtOpinionsSpecialist()
        agent.client = mock_openai_client
        agent.model = "test-model"
        result = agent.extract("ambiguous text")
        assert "confidence" in result
        assert isinstance(result["confidence"], (int, float))


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
