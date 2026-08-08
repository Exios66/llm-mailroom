"""P0.3 — Review resume-lite: an approved review re-invokes the graph starting
at a FRESH extraction (never reusing the reviewed extraction data), under the
original doc_id, and archives. Also covers the MemorySaver default checkpointer
and the START entry router."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _resp(content: str) -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


LOW_CLASSIFY = '{"doc_type": "contract", "confidence": 0.40, "reasoning": "Unsure"}'
HIGH_EXTRACT = '{"parties": ["Acme Corp"], "effective_date": "2024-01-01", "confidence": 0.95}'
REPORT_TEXT = "Matter record: Acme Corp service agreement, effective 2024-01-01."


@pytest.fixture
def phased_client(mocker):
    """Mock LLM client with a scripted sequence of responses: classify low →
    retry low → (resume) fresh extract high → report text."""
    contents = [LOW_CLASSIFY, LOW_CLASSIFY, HIGH_EXTRACT, REPORT_TEXT]
    client = MagicMock()
    client.chat.completions.create.side_effect = [_resp(c) for c in contents]
    mocker.patch("llm.client.OpenAI", return_value=client)
    mocker.patch(
        "agents.base.BaseAgent.__init__",
        lambda self, mock=client: setattr(self, "client", mock) or setattr(self, "model", "test-model"),
    )
    return client


def _run_to_review(temp_base_dir, phased_client) -> dict:
    from graph.build_graph import run_pipeline

    inbox = temp_base_dir / "pipeline" / "inbox"
    test_file = inbox / "resume_test.txt"
    test_file.write_text("Service agreement between Acme Corp and Beta LLC.")

    result = run_pipeline(test_file, "MATTER-RESUME")
    assert result.get("stage") == "review"
    return result


class TestReviewResume:
    def test_entry_route_resume_skips_ingest_and_classify(self):
        from graph.build_graph import entry_route

        assert entry_route({"resume_extraction": True, "doc_type": "contract"}) == "ingest"
        # The approved-guard is deliberate: only the resume-from-review path
        # sets review_decision, so a crashed/partial run can never skip
        # classification.
        assert (
            entry_route(
                {"resume_extraction": True, "review_decision": "approved", "doc_type": "contract"}
            )
            == "extract"
        )
        assert entry_route({"resume_extraction": True, "doc_type": None}) == "ingest"
        assert entry_route({"resume_extraction": False}) == "ingest"
        assert entry_route({}) == "ingest"

    def test_default_checkpointer_is_memory(self):
        from langgraph.checkpoint.memory import MemorySaver
        from graph.build_graph import build_graph

        graph = build_graph()
        assert isinstance(graph.checkpointer, MemorySaver)

    def test_resume_approved_archives_with_fresh_extraction(
        self, temp_base_dir, phased_client
    ):
        from pipeline.bins import review_dir, load_manifest, manifests_dir, archive_dir
        from graph.build_graph import resume_from_review

        # Phase 1: low confidence → review (classify + retry use the first two
        # scripted responses).
        result = _run_to_review(temp_base_dir, phased_client)
        doc_id = result["doc_id"]
        manifest = load_manifest(doc_id)
        assert manifest is not None
        assert manifest.doc_type == "contract"
        review_file = review_dir() / manifest.original_filename
        assert review_file.exists()

        # Phase 2: approve → fresh extraction (extract + reporter use the next
        # two scripted responses).
        resumed = resume_from_review(manifest, review_file)

        assert resumed.get("stage") == "archived"
        assert resumed.get("doc_id") == doc_id  # original doc_id preserved
        assert resumed.get("doc_type") == "contract"
        assert resumed.get("extraction_confidence") == 0.95
        assert resumed.get("extraction_attempts") == 1  # fresh, single attempt
        assert resumed.get("extracted_data", {}).get("parties") == ["Acme Corp"]

        # The review bin file moved to the archive under the original doc_id.
        assert not review_file.exists()
        archived = archive_dir("MATTER-RESUME", "contract") / manifest.original_filename
        assert archived.exists()

        # Manifest updated to ARCHIVED (same doc_id — audit chain intact).
        updated = load_manifest(doc_id)
        assert updated.stage.value == "archived"
        assert updated.review_decision == "approved"

    def test_resume_requires_classification(self, temp_base_dir):
        from graph.build_graph import resume_from_review
        from schemas.manifest import DocumentManifest

        manifest = DocumentManifest(
            doc_id="no-class",
            matter_id="M",
            original_filename="x.txt",
            doc_type=None,
        )
        with pytest.raises(ValueError, match="no classification"):
            resume_from_review(manifest, Path("/nonexistent/review/x.txt"))
