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

    def test_graph_routes_low_confidence_to_review(self, temp_base_dir, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"doc_type": "contract", "confidence": 0.40, "reasoning": "Unsure"}'
        )
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
        assert result.get("stage") == "review"

    def test_ingest_node_creates_manifest(self, temp_base_dir):
        from graph.build_graph import ingest_node, _ensure_dirs
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

        result = ingest_node(state)
        assert result["doc_id"] != ""
        assert result["stage"] == "processing"
        assert result["doc_text"] == "Test ingest content."

    def test_pipeline_completes_with_mocked_llm(self, temp_base_dir, mock_openai_client):
        mock_openai_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"doc_type": "correspondence", "confidence": 0.88, "reasoning": "Legal letter"}'
        )
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
