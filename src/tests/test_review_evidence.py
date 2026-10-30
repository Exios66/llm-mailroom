"""Durable review evidence and human override for parked documents.

A decision model writes no reasoning, so a parked document carries its
reviewer opinion as structured fields on the manifest and under
``extracted_data["_review"]`` in the catalog. A human's class override must
reach extract through the ``interrupt()`` resume, including after a restart.

Network-free: the decision backend is a fake and the sorter is the canned
LangChain fake from ``conftest``.
"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agents.decision_backend import register_decision_backend
from pipeline.decision_review import DecisionAnswer

SORTER_LABEL = "insurance_claim"
REVIEWER_LABEL = "corporate_record"
DECISION_ONLY = {
    "insurance_claim": {"mode": "decision_only", "cutoff": 0.9},
    "corporate_record": {"mode": "decision_only", "cutoff": 0.9},
}
REVIEW_FIELDS = (
    "review_causes",
    "review_verdict",
    "reviewer_doc_type",
    "reviewer_doc_subclass",
    "reviewer_confidence",
    "reviewer_model",
)


@pytest.fixture(autouse=True)
def _clean_backend():
    register_decision_backend(None)
    yield
    register_decision_backend(None)


@pytest.fixture
def decision_lane(monkeypatch, mock_langchain_llm):
    """Sorter says insurance_claim at medium confidence; the decision model
    says corporate_record with high confidence, so the document parks."""
    import pipeline.config as config

    cfg = copy.deepcopy(config.load_config())
    cfg["reviewer"] = {"default_mode": "llm", "by_class": copy.deepcopy(DECISION_ONLY)}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")

    mock_langchain_llm.classification = {
        "doc_type": SORTER_LABEL,
        "contract_subtype": None,
        "doc_subclass": "carrier",
        "confidence": 0.93,
        "reasoning": "medium",
    }
    register_decision_backend(
        lambda text, doc_types, catalogs: DecisionAnswer(
            REVIEWER_LABEL, "bylaws", 0.95, "decision-model-1"
        )
    )
    return mock_langchain_llm


@pytest.fixture
def extractors(monkeypatch):
    """Record which specialist extract dispatches to (no LLM)."""
    import graph.build_graph as bg

    called = []

    def make(label, payload):
        def extract(doc_text, pages=None, handoff_context=None):
            called.append(label)
            return {**payload, "confidence": 0.99}

        return extract

    monkeypatch.setattr(
        bg,
        "_extract_insurance_claims",
        make("insurance_claim", {"claim_number": "C-1", "insured_party": "Acme Corp"}),
    )
    monkeypatch.setattr(
        bg,
        "_extract_corporate_records",
        make("corporate_record", {"entity_name": "Acme Corp", "record_type": "bylaws"}),
    )
    return called


def _park(temp_base_dir):
    from graph.build_graph import run_pipeline

    test_file = temp_base_dir / "pipeline" / "inbox" / "evidence.txt"
    test_file.write_text("Minutes of the board of Acme Corp, annual meeting.")
    result = run_pipeline(test_file, "MATTER-EVIDENCE")
    assert result.get("stage") == "review"
    return result


def _archived_files(temp_base_dir):
    root = temp_base_dir / "pipeline" / "archive"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _catalog_row(doc_id):
    from storage.catalog import get_document
    from storage.db import ensure_schema

    ensure_schema()
    return asyncio.run(get_document(doc_id))


def test_parked_manifest_keeps_both_opinions(temp_base_dir, decision_lane):
    from pipeline.bins import load_manifest

    result = _park(temp_base_dir)
    manifest = load_manifest(result["doc_id"])

    assert manifest.doc_type == SORTER_LABEL
    assert manifest.reviewer_doc_type == REVIEWER_LABEL
    assert manifest.reviewer_doc_subclass == "bylaws"
    assert manifest.reviewer_confidence == 0.95
    assert manifest.reviewer_model == "decision-model-1"
    assert manifest.review_verdict == "decision_escalates"
    assert "reviewer_disagreement" in manifest.review_causes

    review = _catalog_row(result["doc_id"]).extracted_data["_review"]
    assert review["reviewer_doc_type"] == REVIEWER_LABEL
    assert review["reviewer_model"] == "decision-model-1"
    assert "reviewer_disagreement" in review["review_causes"]


@pytest.mark.parametrize(
    "bad",
    ["high", [0.9], float("nan"), True, 10**400],
    ids=["text", "list", "nan", "bool", "huge"],
)
def test_invalid_confidence_parks_for_review(temp_base_dir, decision_lane, bad):
    """Through human_review_node: the manifest must validate, not fail the run."""
    from pipeline.bins import load_manifest

    register_decision_backend(
        lambda text, doc_types, catalogs: DecisionAnswer(
            SORTER_LABEL, "carrier", bad, "decision-model-1"
        )
    )
    result = _park(temp_base_dir)
    manifest = load_manifest(result["doc_id"])

    assert manifest.stage.value == "review"
    assert manifest.reviewer_confidence is None
    assert manifest.review_verdict == "decision_escalates"
    assert "reviewer_low_confidence" in manifest.review_causes


def test_old_manifest_without_review_fields_still_loads():
    from schemas.manifest import DocumentManifest

    old = {"doc_id": "old-1", "matter_id": "M", "original_filename": "a.txt", "stage": "review"}
    manifest = DocumentManifest.model_validate_json(json.dumps(old))
    for name in REVIEW_FIELDS:
        assert getattr(manifest, name) is None


def test_queue_row_exposes_review_fields():
    from pipeline.review_resolve import serialize_document
    from schemas.manifest import DocumentManifest

    manifest = DocumentManifest(
        doc_id="q-1",
        matter_id="M",
        original_filename="a.txt",
        doc_type=SORTER_LABEL,
        review_causes=["reviewer_disagreement"],
        review_verdict="decision_escalates",
        reviewer_doc_type=REVIEWER_LABEL,
        reviewer_doc_subclass="bylaws",
        reviewer_confidence=0.95,
        reviewer_model="decision-model-1",
    )
    row = serialize_document(manifest)
    assert row["review_causes"] == ["reviewer_disagreement"]
    assert row["review_verdict"] == "decision_escalates"
    assert row["reviewer_doc_type"] == REVIEWER_LABEL
    assert row["reviewer_doc_subclass"] == "bylaws"
    assert row["reviewer_confidence"] == 0.95
    assert row["reviewer_model"] == "decision-model-1"

    # A catalog row carries the same data under extracted_data["_review"].
    catalog = SimpleNamespace(
        doc_id="q-2",
        extracted_data={"_review": {"reviewer_doc_type": REVIEWER_LABEL, "reviewer_model": "m"}},
    )
    row = serialize_document(catalog)
    assert row["reviewer_doc_type"] == REVIEWER_LABEL
    assert row["reviewer_model"] == "m"
    assert row["review_causes"] is None

    # Documents with no review evidence serialize the keys as None.
    bare = serialize_document(SimpleNamespace(doc_id="q-3", extracted_data=None))
    for name in REVIEW_FIELDS:
        assert bare[name] is None


def _override_and_resume(doc_id):
    from graph.build_graph import resume_from_review
    from pipeline.bins import load_manifest, review_file_path
    from pipeline.review_resolve import apply_classification_override

    manifest = load_manifest(doc_id)
    review_file = review_file_path(manifest)
    apply_classification_override(manifest, override_doc_type=REVIEWER_LABEL)
    return resume_from_review(manifest, review_file)


def test_override_reaches_extract_via_interrupt(temp_base_dir, decision_lane, extractors):
    from graph.build_graph import _thread_is_interrupted, get_compiled_graph

    result = _park(temp_base_dir)
    assert _thread_is_interrupted(get_compiled_graph(), result["checkpoint_thread_id"])

    resumed = _override_and_resume(result["doc_id"])

    assert resumed["stage"] == "archived"
    assert resumed["doc_type"] == REVIEWER_LABEL
    assert extractors == [REVIEWER_LABEL]
    assert resumed["classification_confidence"] is None
    _assert_archived_review_evidence(result["doc_id"])


def _assert_archived_review_evidence(doc_id):
    from pipeline.bins import load_manifest

    manifest = load_manifest(doc_id)
    review = _catalog_row(doc_id).extracted_data["_review"]
    assert manifest.review_verdict == review["review_verdict"] == "decision_escalates"
    assert manifest.reviewer_model == review["reviewer_model"] == "decision-model-1"
    assert manifest.reviewer_doc_type == review["reviewer_doc_type"] == REVIEWER_LABEL
    assert "reviewer_disagreement" in manifest.review_causes
    assert "reviewer_disagreement" in review["review_causes"]


def test_override_survives_restart(temp_base_dir, decision_lane, monkeypatch, extractors):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from graph.build_graph import reset_compiled_graph

    monkeypatch.setenv("MAILROOM_CHECKPOINTER", "sqlite")
    reset_compiled_graph()
    result = _park(temp_base_dir)

    # Restart: a new compiled graph over the same on-disk checkpoints.
    reset_compiled_graph()
    resumed = _override_and_resume(result["doc_id"])

    assert resumed["stage"] == "archived"
    assert resumed["doc_type"] == REVIEWER_LABEL
    assert extractors == [REVIEWER_LABEL]


def test_rejected_never_reaches_archive(temp_base_dir, decision_lane):
    from langgraph.types import Command

    from graph.build_graph import _execute_run

    result = _park(temp_base_dir)
    doc_id = result["doc_id"]
    thread_id = result["checkpoint_thread_id"]

    rejected = _execute_run(
        {"doc_id": doc_id, "matter_id": "MATTER-EVIDENCE", "checkpoint_thread_id": thread_id},
        seed="evidence",
        attempt=0,
        thread_id=thread_id,
        invoke_input=Command(resume={"decision": "rejected", "notes": "no"}),
        trace_input={"resumed": True},
    )

    assert rejected["stage"] == "failed"
    assert _archived_files(temp_base_dir) == []
    row = _catalog_row(doc_id)
    assert row.stage == "failed"
    assert row.stage != "archived"
    assert row.extracted_data["_review"]["reviewer_model"] == "decision-model-1"
    assert "reviewer_disagreement" in row.extracted_data["_review"]["review_causes"]


def test_rejection_preserves_parked_extraction(temp_base_dir, monkeypatch):
    import graph.build_graph as bg

    source = temp_base_dir / "rejected.txt"
    source.write_text("document")
    monkeypatch.setattr(bg, "interrupt", lambda payload: "rejected")
    result = bg.human_review_node({
        "doc_id": "rejected-with-extraction", "matter_id": "M",
        "original_filename": source.name, "file_path": str(source),
        "doc_type": SORTER_LABEL, "extracted_data": {"claim_number": "C-1"},
        "review_verdict": "decision_escalates", "reviewer_model": "decision-model-1",
        "reviewer_causes": ["reviewer_disagreement"],
    })
    assert result["stage"] == "failed"
    extracted = _catalog_row("rejected-with-extraction").extracted_data
    assert extracted["claim_number"] == "C-1"
    assert extracted["_review"]["reviewer_model"] == "decision-model-1"
    assert "reviewer_disagreement" in extracted["_review"]["review_causes"]


def test_fallback_resume_that_archives_keeps_review_evidence(
    temp_base_dir, decision_lane, extractors
):
    from graph.build_graph import reset_compiled_graph

    result = _park(temp_base_dir)
    reset_compiled_graph()
    resumed = _override_and_resume(result["doc_id"])
    assert resumed["stage"] == "archived"
    _assert_archived_review_evidence(result["doc_id"])


def test_fallback_resume_that_reparks_keeps_both_opinions(
    temp_base_dir, decision_lane, extractors, monkeypatch
):
    """After a restart with the default MemorySaver the interrupt checkpoint is
    gone, so resume takes the stateless fallback. If that run parks again, the
    new manifest and catalog row must still carry both opinions and causes."""
    import graph.build_graph as bg
    from pipeline.bins import load_manifest, review_file_path
    from pipeline.review_resolve import apply_classification_override

    result = _park(temp_base_dir)
    doc_id = result["doc_id"]
    parked = load_manifest(doc_id)
    causes_before = list(parked.review_causes)
    assert "reviewer_disagreement" in causes_before

    # Restart: the in-memory checkpoint is gone.
    bg.reset_compiled_graph()
    assert not bg._thread_is_interrupted(bg.get_compiled_graph(), parked.checkpoint_thread_id)

    # The corrected run still cannot extract, so it parks again.
    monkeypatch.setattr(
        bg, "_extract_corporate_records", lambda *a, **k: {"confidence": 0.1}
    )
    manifest = load_manifest(doc_id)
    review_file = review_file_path(manifest)
    apply_classification_override(manifest, override_doc_type=REVIEWER_LABEL)
    resumed = bg.resume_from_review(manifest, review_file)
    assert resumed["stage"] == "review"

    again = load_manifest(doc_id)
    assert again.doc_type == REVIEWER_LABEL
    assert again.reviewer_doc_type == REVIEWER_LABEL
    assert again.reviewer_doc_subclass == "bylaws"
    assert again.reviewer_confidence == 0.95
    assert again.reviewer_model == "decision-model-1"
    assert again.review_verdict == "decision_escalates"
    assert "reviewer_disagreement" in again.review_causes

    review = _catalog_row(doc_id).extracted_data["_review"]
    assert review["reviewer_model"] == "decision-model-1"
    assert "reviewer_disagreement" in review["review_causes"]


def _assert_row_keeps_evidence(doc_id):
    review = _catalog_row(doc_id).extracted_data["_review"]
    assert review["reviewer_doc_type"] == REVIEWER_LABEL
    assert review["reviewer_model"] == "decision-model-1"
    assert review["review_verdict"] == "decision_escalates"
    assert "reviewer_disagreement" in review["review_causes"]


def _assert_archived_evidence(doc_id):
    from pipeline.bins import load_manifest

    manifest = load_manifest(doc_id)
    assert manifest.stage.value == "archived"
    assert manifest.review_verdict == "decision_escalates"
    assert manifest.reviewer_model == "decision-model-1"
    assert manifest.reviewer_doc_type == REVIEWER_LABEL
    assert "reviewer_disagreement" in manifest.review_causes
    row = _catalog_row(doc_id)
    assert row.stage == "archived"
    assert row.extracted_data["entity_name"] == "Acme Corp"
    _assert_row_keeps_evidence(doc_id)


def test_graph_rejection_keeps_evidence_in_catalog(temp_base_dir, decision_lane):
    from langgraph.types import Command

    from graph.build_graph import _execute_run

    result = _park(temp_base_dir)
    doc_id = result["doc_id"]
    thread_id = result["checkpoint_thread_id"]

    _execute_run(
        {"doc_id": doc_id, "matter_id": "MATTER-EVIDENCE", "checkpoint_thread_id": thread_id},
        seed="evidence",
        attempt=0,
        thread_id=thread_id,
        invoke_input=Command(resume={"decision": "rejected", "notes": "no"}),
        trace_input={"resumed": True},
    )

    assert _catalog_row(doc_id).stage == "failed"
    _assert_row_keeps_evidence(doc_id)


def _api_client(monkeypatch, temp_base_dir):
    import importlib
    import sys

    from fastapi.testclient import TestClient

    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    if "api.main" in sys.modules:
        importlib.reload(sys.modules["api.main"])
    from api.main import app

    return TestClient(app)


def test_api_rejection_keeps_evidence_in_catalog(temp_base_dir, decision_lane, monkeypatch):
    result = _park(temp_base_dir)
    doc_id = result["doc_id"]

    with _api_client(monkeypatch, temp_base_dir) as client:
        response = client.post(
            f"/review/{doc_id}/resolve",
            headers={"Authorization": "Bearer test-token-123"},
            json={"decision": "rejected", "disposition": "resume"},
        )

    assert response.status_code == 200
    assert _catalog_row(doc_id).stage == "failed"
    _assert_row_keeps_evidence(doc_id)


def test_operator_completion_keeps_evidence_in_catalog(temp_base_dir, decision_lane, monkeypatch):
    result = _park(temp_base_dir)
    doc_id = result["doc_id"]

    with _api_client(monkeypatch, temp_base_dir) as client:
        response = client.post(
            f"/review/{doc_id}/resolve",
            headers={"Authorization": "Bearer test-token-123"},
            json={
                "decision": "approved",
                "disposition": "complete",
                "override_doc_type": REVIEWER_LABEL,
                "extracted_data": {"entity_name": "Acme Corp", "record_type": "bylaws"},
            },
        )

    assert response.status_code == 200, response.text
    _assert_archived_evidence(doc_id)


def test_approved_document_keeps_evidence_through_archive(
    temp_base_dir, decision_lane, extractors
):
    result = _park(temp_base_dir)
    resumed = _override_and_resume(result["doc_id"])
    assert resumed["stage"] == "archived"
    _assert_archived_evidence(result["doc_id"])


def test_approved_evidence_survives_sqlite_restart(
    temp_base_dir, decision_lane, monkeypatch, extractors
):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from graph.build_graph import reset_compiled_graph

    monkeypatch.setenv("MAILROOM_CHECKPOINTER", "sqlite")
    reset_compiled_graph()
    result = _park(temp_base_dir)

    reset_compiled_graph()
    resumed = _override_and_resume(result["doc_id"])
    assert resumed["stage"] == "archived"
    _assert_archived_evidence(result["doc_id"])


def test_approved_evidence_survives_stateless_resume(temp_base_dir, decision_lane, extractors):
    """MemorySaver restart: the fallback run rebuilds state from the manifest."""
    import graph.build_graph as bg

    result = _park(temp_base_dir)
    bg.reset_compiled_graph()
    resumed = _override_and_resume(result["doc_id"])
    assert resumed["stage"] == "archived"
    _assert_archived_evidence(result["doc_id"])


def test_auto_accepted_document_keeps_verdict_through_archive(
    temp_base_dir, decision_lane, extractors
):
    from graph.build_graph import run_pipeline
    from pipeline.bins import load_manifest

    register_decision_backend(
        lambda text, doc_types, catalogs: DecisionAnswer(
            SORTER_LABEL, "carrier", 0.95, "decision-model-1"
        )
    )
    test_file = temp_base_dir / "pipeline" / "inbox" / "accepted.txt"
    test_file.write_text("Claim number 123 for water damage.")
    result = run_pipeline(test_file, "MATTER-EVIDENCE")

    assert result["stage"] == "archived"
    manifest = load_manifest(result["doc_id"])
    assert manifest.review_verdict == "decision_accepts"
    assert manifest.reviewer_model == "decision-model-1"
    assert manifest.review_causes is None
    review = _catalog_row(result["doc_id"]).extracted_data["_review"]
    assert review["review_verdict"] == "decision_accepts"


def test_document_without_reviewer_gets_no_review_block():
    from pipeline.review_resolve import with_review_evidence
    from schemas.manifest import DocumentManifest

    manifest = DocumentManifest(doc_id="n-1", matter_id="M", original_filename="a.txt")
    data = {"entity_name": "Acme Corp"}
    assert with_review_evidence(data, manifest) is data
    assert with_review_evidence(None, manifest) is None


def test_review_block_stays_out_of_matter_context(monkeypatch):
    """Archived evidence must not change what the Boss LLM is shown."""
    import graph.build_graph as bg

    row = SimpleNamespace(
        doc_id="other",
        doc_type="contract",
        stage="archived",
        extracted_data={"parties": ["A"], "_review": {"review_verdict": "decision_accepts"}},
    )
    monkeypatch.setattr(bg, "_run_coro", lambda coro: [row])

    context = bg._fetch_matter_context({"matter_id": "M", "doc_id": "d1"})

    assert context[0]["extracted_data"] == {"parties": ["A"]}
