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
HIGH_EXTRACT = '{"parties": ["Acme Corp"], "effective_date": "2024-01-01", "confidence": 0.99}'


@pytest.fixture
def phased_client(mocker, mock_langchain_llm):
    """Mock LLM clients with a scripted sequence: the LangChain sorter returns
    low confidence twice (classify → retry → review); on resume the contracts
    specialist returns a high-confidence extraction. compile_report is
    procedural (no LLM)."""
    mock_langchain_llm.classification = {
        "doc_type": "contract",
        "contract_subtype": "other",
        "confidence": 0.40,
        "reasoning": "Unsure",
    }
    mock_langchain_llm.extraction = {
        "parties": ["Acme Corp"],
        "effective_date": "2024-01-01",
        "confidence": 0.99,
    }
    client = MagicMock()
    # Lane B (judge/arbiter) should not run when extract ≥ class high (0.98);
    # keep a spare response in case a review-path agent still calls get_llm.
    client.chat.completions.create.side_effect = [
        _resp('{"verdict": "complete", "score": 1.0, "findings": []}')
    ]
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
    def test_entry_route_resume_skips_intake_and_classify(self):
        from graph.build_graph import entry_route

        assert entry_route({"resume_extraction": True, "doc_type": "contract"}) == "intake"
        # The approved-guard is deliberate: only the resume-from-review path
        # sets review_decision, so a crashed/partial run can never skip
        # classification.
        assert (
            entry_route(
                {"resume_extraction": True, "review_decision": "approved", "doc_type": "contract"}
            )
            == "extract"
        )
        assert entry_route({"resume_extraction": True, "doc_type": None}) == "intake"
        assert entry_route({"resume_extraction": False}) == "intake"
        assert entry_route({}) == "intake"

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

        # Phase 2: approve → fresh extraction (procedural assemble → archive).
        resumed = resume_from_review(manifest, review_file)

        assert resumed.get("stage") == "archived"
        assert resumed.get("doc_id") == doc_id  # original doc_id preserved
        assert resumed.get("doc_type") == "contract"
        assert resumed.get("extraction_confidence") == 0.99
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

    def test_resume_soft_miss_reparks_review_not_failed(
        self, temp_base_dir, mocker, mock_langchain_llm
    ):
        """Post-HITL extract that still misses tight gates returns to review,
        never auto-failed solely because the doc once needed a human."""
        from pipeline.bins import review_dir, load_manifest, failed_dir
        from graph.build_graph import resume_from_review

        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "other",
            "confidence": 0.40,
            "reasoning": "Unsure",
        }
        mock_langchain_llm.extraction = {
            "parties": ["Acme Corp"],
            "effective_date": "2024-01-01",
            "confidence": 0.91,  # contract low≤x<judge_band_high → Lane B
        }
        client = MagicMock()
        # Judge incomplete + arbiter human_review → re-park review.
        client.chat.completions.create.side_effect = [
            _resp(
                '{"label": "incomplete", "score": 0.2, '
                '"findings": ["missing governing_law"], "reasoning": "gap"}'
            ),
            _resp(
                '{"decision": "human_review", "reasoning": "blocking gap", '
                '"fields_to_fix": ["governing_law"], "handoff_notes": "need law"}'
            ),
        ]
        mocker.patch("llm.client.OpenAI", return_value=client)
        mocker.patch(
            "agents.base.BaseAgent.__init__",
            lambda self, mock=client: setattr(self, "client", mock)
            or setattr(self, "model", "test-model"),
        )

        result = _run_to_review(temp_base_dir, client)
        doc_id = result["doc_id"]
        manifest = load_manifest(doc_id)
        review_file = review_dir() / manifest.original_filename
        resumed = resume_from_review(manifest, review_file)
        assert resumed.get("stage") == "review"
        assert not (failed_dir() / manifest.original_filename).exists()
        assert (review_dir() / manifest.original_filename).exists()

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

    def test_park_pauses_via_interrupt_and_stores_thread_id(
        self, temp_base_dir, phased_client
    ):
        from pipeline.bins import review_dir, load_manifest
        from graph.build_graph import get_compiled_graph, _thread_is_interrupted

        result = _run_to_review(temp_base_dir, phased_client)
        assert result.get("stage") == "review"
        assert result.get("review_decision") == "pending_review"
        thread_id = result.get("checkpoint_thread_id")
        assert thread_id
        manifest = load_manifest(result["doc_id"])
        assert manifest.checkpoint_thread_id == thread_id
        assert (review_dir() / manifest.original_filename).exists()
        graph = get_compiled_graph()
        assert _thread_is_interrupted(graph, thread_id)

    def test_park_for_review_is_idempotent(self, temp_base_dir):
        from pipeline.bins import park_for_review, review_dir
        from schemas.manifest import DocumentManifest, PipelineStage

        src = temp_base_dir / "pipeline" / "processing" / "w1"
        src.mkdir(parents=True)
        f = src / "park.txt"
        f.write_text("hello")
        manifest = DocumentManifest(
            doc_id="park-1",
            matter_id="M",
            original_filename="park.txt",
            stage=PipelineStage.REVIEW,
        )
        dest, newly = park_for_review(f, manifest)
        assert newly is True
        assert dest == review_dir() / "park.txt"
        assert dest.exists()
        assert not f.exists()
        dest2, newly2 = park_for_review(f, manifest)
        assert newly2 is False
        assert dest2 == dest
        assert dest.exists()
