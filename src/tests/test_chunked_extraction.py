"""Chunked extraction for every live specialist, not contracts-only."""

from unittest.mock import MagicMock


def test_all_live_specialists_route_through_chunked(monkeypatch):
    from graph import build_graph as bg

    seen: list[str] = []

    def fake(agent_fn, doc_text, pages, handoff_context):
        seen.append(getattr(agent_fn, "__name__", str(agent_fn)))
        return {"confidence": 0.9}

    monkeypatch.setattr(bg, "_run_chunked_extraction", fake)
    bg._extract_contracts("x", None, None)
    bg._extract_corporate_records("x", None, None)
    bg._extract_correspondence("x", None, None)
    bg._extract_compliance("x", None, None)
    bg._extract_insurance_claims("x", None, None)
    assert seen == [
        "ContractsSpecialist",
        "CorporateRecordsSpecialist",
        "CorrespondenceSpecialist",
        "ComplianceSpecialist",
        "InsuranceClaimsSpecialist",
    ]


def test_extract_chunked_splits_long_non_contract(mock_openai_client):
    from agents.compliance_specialist import ComplianceSpecialist

    agent = ComplianceSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    calls: list[int] = []

    def fake_extract(doc_text, pages=None, handoff_context=None):
        calls.append(len(doc_text))
        return {
            "filing_type": "10-k",
            "confidence": 0.9,
            "key_provisions": [f"chunk-{len(calls)}"],
        }

    agent.extract = fake_extract
    text = "Filing paragraph.\n\n" * 80
    result = agent.extract_chunked(text, chunk_chars=400, overlap_chars=40)
    assert len(calls) > 1
    assert result.get("confidence") == 0.9
    assert "chunk-1" in (result.get("key_provisions") or [])


def test_extract_chunked_short_document_single_pass(mock_openai_client):
    from agents.insurance_claims_specialist import InsuranceClaimsSpecialist

    agent = InsuranceClaimsSpecialist()
    agent.client = mock_openai_client
    agent.model = "test-model"
    calls: list[str] = []

    def fake_extract(doc_text, pages=None, handoff_context=None):
        calls.append(doc_text)
        return {"claim_number": "1", "confidence": 0.8}

    agent.extract = fake_extract
    short = "FNOL for hail damage."
    result = agent.extract_chunked(short, chunk_chars=90_000, overlap_chars=8_000)
    assert calls == [short]
    assert result == {"claim_number": "1", "confidence": 0.8}


def test_retry_extract_does_not_hard_truncate_doc_text():
    import inspect
    from graph.build_graph import retry_extract_node

    source = inspect.getsource(retry_extract_node)
    assert "doc_text[:25000]" not in source
    assert "extractor(doc_text, doc_pages, handoff_context)" in source


def test_chunk_windows_capped_at_agent_budget(monkeypatch):
    from graph import build_graph as bg

    captured: dict = {}

    class FakeAgent:
        def _configured_max_input_chars(self):
            return 4_000

        def extract_chunked(self, doc_text, chunk_chars=90_000, overlap_chars=8_000, pages=None, handoff_context=None):
            captured["chunk_chars"] = chunk_chars
            captured["overlap_chars"] = overlap_chars
            return {"confidence": 0.5}

    monkeypatch.setattr(bg, "_instantiate_specialist", lambda fn, ctx: FakeAgent())
    monkeypatch.setattr(bg, "_chunk_config", lambda: {"enabled": True, "chunk_chars": 90_000, "overlap_chars": 8_000})
    bg._run_chunked_extraction(MagicMock, "hello", None, None)
    assert captured["chunk_chars"] <= 4_000
    assert captured["overlap_chars"] < captured["chunk_chars"]
