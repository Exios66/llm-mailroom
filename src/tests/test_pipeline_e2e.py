import pytest
from pathlib import Path
from graph.state import DocumentState


class TestPipelineE2E:
    def test_graph_builds_and_runs_basic(self, temp_base_dir, mock_openai_client):
        from graph.build_graph import build_graph, _ensure_dirs
        _ensure_dirs()

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "test_doc.txt"
        test_file.write_text("Sample contract document for testing purposes.")

        graph = build_graph()
        config = {"configurable": {"thread_id": "e2e-test-1"}}

        initial_state: DocumentState = {
            "doc_id": "",
            "matter_id": "TEST-MATTER",
            "original_filename": "test_doc.txt",
            "stage": "inbox",
            "doc_type": None,
            "classification_confidence": None,
            "classification_attempts": 0,
            "extracted_data": None,
            "extraction_confidence": None,
            "extraction_attempts": 0,
            "trace_id": None,
            "escalation_reason": None,
            "review_decision": None,
            "retry_count": 0,
            "conflict_detected": False,
            "file_path": str(test_file),
            "doc_text": "",
            "error_message": None,
            "messages": [],
        }

        result = graph.invoke(initial_state, config)
        assert result.get("doc_id") != ""
        assert result.get("stage") == "archived"

    def test_graph_routes_low_confidence_to_review(
        self, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "other",
            "confidence": 0.40,
            "reasoning": "Unsure",
        }
        from graph.build_graph import build_graph
        _ensure_dirs_relative(temp_base_dir)

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "ambiguous.txt"
        test_file.write_text("Ambiguous content that confuses the classifier.")

        graph = build_graph()
        config = {"configurable": {"thread_id": "e2e-low-conf"}}

        initial_state: DocumentState = {
            "doc_id": "",
            "matter_id": "TEST-MATTER",
            "original_filename": "ambiguous.txt",
            "stage": "inbox",
            "doc_type": None,
            "classification_confidence": None,
            "classification_attempts": 0,
            "extracted_data": None,
            "extraction_confidence": None,
            "extraction_attempts": 0,
            "trace_id": None,
            "escalation_reason": None,
            "review_decision": None,
            "retry_count": 0,
            "conflict_detected": False,
            "file_path": str(test_file),
            "doc_text": "",
            "error_message": None,
            "messages": [],
        }

        result = graph.invoke(initial_state, config)
        assert result.get("__interrupt__") or result.get("stage") == "review"
        from pipeline.bins import review_dir
        assert (review_dir() / "ambiguous.txt").exists()

    def test_graph_routes_medium_confidence_to_review(
        self, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        # Classified but not clearly confident (low <= confidence < high).
        # KANBAN-062 (Lane A): the medium band no longer goes straight to
        # human review — it gets an INDEPENDENT agent second opinion
        # (review_classify). With the mocked reviewer confident (contract @
        # 0.95 via mock_openai_client), the lane resolves the ambiguity
        # automatically: the reviewer's winning label is applied and the
        # document proceeds through extraction to archive. A still-ambiguous
        # reviewer would escalate to human review with both opinions
        # preserved (covered at node level in test_lanes_062_063.py).
        mock_langchain_llm.classification = {
            "doc_type": "correspondence",
            "contract_subtype": None,
            "doc_subclass": "memo",
            "confidence": 0.90,
            "reasoning": "Multi-topic memo",
        }
        from graph.build_graph import build_graph
        _ensure_dirs_relative(temp_base_dir)

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "multi_topic_memo.txt"
        test_file.write_text("Memo spanning service agreements, filings, and a demand letter.")

        graph = build_graph()
        config = {"configurable": {"thread_id": "e2e-med-conf"}}

        initial_state: DocumentState = {
            "doc_id": "",
            "matter_id": "TEST-MATTER",
            "original_filename": "multi_topic_memo.txt",
            "stage": "inbox",
            "doc_type": None,
            "classification_confidence": None,
            "classification_attempts": 0,
            "extracted_data": None,
            "extraction_confidence": None,
            "extraction_attempts": 0,
            "trace_id": None,
            "escalation_reason": None,
            "review_decision": None,
            "retry_count": 0,
            "conflict_detected": False,
            "file_path": str(test_file),
            "doc_text": "",
            "error_message": None,
            "messages": [],
        }

        result = graph.invoke(initial_state, config)
        # The lane fired and the confident reviewer won:
        assert result.get("review_verdict") == "reviewer_overrides"
        assert result.get("reviewer_doc_type") == "contract"
        assert result.get("reviewer_confidence") is not None
        # The reviewer's winning label was APPLIED to the live state:
        assert result.get("doc_type") == "contract"
        # ...and the document flowed on through extraction to archive:
        assert result.get("stage") == "archived"

    def test_intake_node_creates_manifest(self, temp_base_dir):
        from graph.build_graph import intake_node, _ensure_dirs
        _ensure_dirs()

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "ingest_test.txt"
        test_file.write_text("Test ingest content.")

        state: DocumentState = {
            "doc_id": "",
            "matter_id": "TEST",
            "original_filename": "ingest_test.txt",
            "stage": "inbox",
            "doc_type": None,
            "classification_confidence": None,
            "classification_attempts": 0,
            "extracted_data": None,
            "extraction_confidence": None,
            "extraction_attempts": 0,
            "trace_id": None,
            "escalation_reason": None,
            "review_decision": None,
            "retry_count": 0,
            "conflict_detected": False,
            "file_path": str(test_file),
            "doc_text": "",
            "error_message": None,
            "messages": [],
        }

        result = intake_node(state)
        assert result["doc_id"] != ""
        assert result["stage"] == "processing"
        assert result["doc_text"] == "Test ingest content."

    def test_pipeline_completes_with_mocked_llm(
        self, temp_base_dir, mock_openai_client, mock_langchain_llm
    ):
        mock_langchain_llm.classification = {
            "doc_type": "correspondence",
            "contract_subtype": None,
            "doc_subclass": "letter",
            "confidence": 0.96,
            "reasoning": "Legal letter",
        }
        from graph.build_graph import build_graph
        _ensure_dirs_relative(temp_base_dir)

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "letter.txt"
        test_file.write_text("Demand letter from opposing counsel regarding contractual dispute.")

        graph = build_graph()
        config = {"configurable": {"thread_id": "e2e-complete"}}
        initial_state: DocumentState = {
            "doc_id": "",
            "matter_id": "MATTER-001",
            "original_filename": "letter.txt",
            "stage": "inbox",
            "doc_type": None,
            "classification_confidence": None,
            "classification_attempts": 0,
            "extracted_data": None,
            "extraction_confidence": None,
            "extraction_attempts": 0,
            "trace_id": None,
            "escalation_reason": None,
            "review_decision": None,
            "retry_count": 0,
            "conflict_detected": False,
            "file_path": str(test_file),
            "doc_text": "",
            "error_message": None,
            "messages": [],
        }
        result = graph.invoke(initial_state, config)
        assert result["stage"] == "archived"


_MINIMAL_TEXT_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 68>>stream
BT /F1 24 Tf 72 720 Td (Service Agreement between Acme and Beta) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000384 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
459
%%EOF
"""


def test_ingest_transcribes_real_pdf_bytes(temp_base_dir, mock_openai_client):
    """Real PDF bytes through ingest (LLM still mocked)."""
    from graph.build_graph import intake_node, _ensure_dirs

    _ensure_dirs()
    inbox = temp_base_dir / "pipeline" / "inbox"
    pdf = inbox / "service.pdf"
    pdf.write_bytes(_MINIMAL_TEXT_PDF)
    result = intake_node(
        {
            "doc_id": "",
            "matter_id": "PDF-MATTER",
            "original_filename": "service.pdf",
            "stage": "inbox",
            "file_path": str(pdf),
            "doc_text": "",
            "classification_attempts": 0,
            "extraction_attempts": 0,
            "retry_count": 0,
            "conflict_detected": False,
            "messages": [],
        }
    )
    assert result.get("doc_id")
    text = result.get("doc_text") or ""
    assert "Service Agreement" in text or "Acme" in text or len(text) > 10


def _ensure_dirs_relative(tmpdir):
    import os
    os.environ["MAILROOM_BASE_DIR"] = str(tmpdir)
    (tmpdir / "pipeline" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmpdir / "pipeline" / "processing").mkdir(parents=True, exist_ok=True)
    (tmpdir / "pipeline" / "classified").mkdir(parents=True, exist_ok=True)
    (tmpdir / "pipeline" / "review").mkdir(parents=True, exist_ok=True)
    (tmpdir / "pipeline" / "failed").mkdir(parents=True, exist_ok=True)
    (tmpdir / "archive").mkdir(parents=True, exist_ok=True)
    (tmpdir / "manifests").mkdir(parents=True, exist_ok=True)
