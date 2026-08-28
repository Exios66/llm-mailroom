"""Producer half of The-Mailroom REVIEW resolve (PR #18) + audit analysis."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def client(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    for mod in ("api.main", "storage.db", "storage.catalog", "storage.audit_log"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from api.main import app

    with TestClient(app) as c:
        yield c


def _auth():
    return {"Authorization": "Bearer test-token-123"}


def _park_review(temp_base_dir, *, doc_id="doc-review-1", doc_type="contract", filename="parked.txt"):
    from pipeline.bins import review_dir, manifests_dir, ensure_dirs
    from schemas.manifest import DocumentManifest, PipelineStage
    from storage.db import ensure_schema
    from storage.catalog import write_document_record
    import asyncio

    ensure_dirs(review_dir(), manifests_dir())
    review_dir().mkdir(parents=True, exist_ok=True)
    (review_dir() / filename).write_text("Service agreement between Acme and Beta.")
    manifest = DocumentManifest(
        doc_id=doc_id,
        matter_id="MATTER-R",
        original_filename=filename,
        stage=PipelineStage.REVIEW,
        doc_type=doc_type,
        classification_confidence=0.4,
        escalation_reason="low_confidence",
        trace_id="trace-abc",
    )
    (manifests_dir() / f"{doc_id}.json").write_text(manifest.model_dump_json(indent=2))
    ensure_schema()
    asyncio.run(
        write_document_record(
            {
                "doc_id": doc_id,
                "matter_id": "MATTER-R",
                "original_filename": filename,
                "stage": "review",
                "doc_type": doc_type,
                "classification_confidence": 0.4,
                "escalation_reason": "low_confidence",
                "trace_id": "trace-abc",
            }
        )
    )
    return manifest


def test_lookup_by_doc_id_trace_and_filename(client, temp_base_dir):
    _park_review(temp_base_dir)
    headers = _auth()
    by_id = client.get("/lookup", params={"doc_id": "doc-review-1"}, headers=headers)
    assert by_id.status_code == 200
    assert by_id.json()["document"]["doc_id"] == "doc-review-1"
    assert by_id.json()["document"]["stage"] == "review"

    by_trace = client.get("/lookup", params={"trace_id": "trace-abc"}, headers=headers)
    assert by_trace.status_code == 200
    assert by_trace.json()["document"]["original_filename"] == "parked.txt"

    by_name = client.get("/lookup", params={"filename": "parked.txt"}, headers=headers)
    assert by_name.status_code == 200
    assert by_name.json()["document"]["doc_id"] == "doc-review-1"

    missing = client.get("/lookup", params={"doc_id": "nope"}, headers=headers)
    assert missing.status_code == 404

    bad = client.get("/lookup", headers=headers)
    assert bad.status_code == 400


def test_review_queue_lists_tray_actions(client, temp_base_dir):
    _park_review(temp_base_dir)
    r = client.get("/review/queue", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["review_queue"] == 1
    assert body["documents"][0]["doc_id"] == "doc-review-1"
    dispositions = {a["disposition"] for a in body["documents"][0]["actions"]}
    assert {"resume", "record", "requeue", "complete"} <= dispositions


def test_resolve_record_and_requeue(client, temp_base_dir):
    from pipeline.bins import inbox_dir

    _park_review(temp_base_dir)
    headers = _auth()
    recorded = client.post(
        "/review/doc-review-1/resolve",
        headers=headers,
        json={"decision": "approved", "disposition": "record", "notes": "paper trail"},
    )
    assert recorded.status_code == 200
    assert recorded.json()["disposition"] == "record"

    requeued = client.post(
        "/v1/review/doc-review-1/resolve",
        headers=headers,
        json={"decision": "rejected", "disposition": "requeue", "notes": "try again"},
    )
    assert requeued.status_code == 200
    body = requeued.json()
    assert body["disposition"] == "requeue"
    assert (inbox_dir() / body["inbox_file"]).exists()


def test_resolve_complete_archives_without_llm(client, temp_base_dir):
    from pipeline.bins import review_dir, archive_dir, load_manifest

    _park_review(temp_base_dir)
    extracted = {"parties": ["Acme"], "effective_date": "2024-01-01", "confidence": 0.99}
    r = client.post(
        "/review/doc-review-1/resolve",
        headers=_auth(),
        json={
            "decision": "approved",
            "disposition": "complete",
            "extracted_data": extracted,
            "notes": "human finished",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["disposition"] == "complete"
    assert r.json()["complete"]["stage"] == "archived"
    assert not (review_dir() / "parked.txt").exists()
    assert (archive_dir("MATTER-R", "contract") / "parked.txt").exists()
    m = load_manifest("doc-review-1")
    assert m.stage.value == "archived"
    assert m.extracted_data["parties"] == ["Acme"]


def test_resolve_resume_override_doc_type_form_compat(client, temp_base_dir, mocker):
    """Legacy form clients still work; override_doc_type enables reroute."""
    _park_review(temp_base_dir, doc_type=None)
    mocker.patch(
        "graph.build_graph.resume_from_review",
        return_value={
            "stage": "archived",
            "doc_type": "correspondence",
            "extraction_confidence": 0.9,
            "extraction_attempts": 1,
        },
    )
    # Without override → 409
    r = client.post(
        "/review/doc-review-1/resolve",
        headers=_auth(),
        data={"decision": "approved", "disposition": "resume"},
    )
    assert r.status_code == 409

    r2 = client.post(
        "/review/doc-review-1/resolve",
        headers=_auth(),
        json={
            "decision": "approved",
            "disposition": "resume",
            "override_doc_type": "correspondence",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["disposition"] == "resume"
    assert r2.json()["resume"]["doc_type"] == "correspondence"


def test_resolve_doc_type_alias_and_requeue_sidecar(client, temp_base_dir, mocker):
    """The-Mailroom PR #20 sends doc_type (not override_doc_type)."""
    from pipeline.bins import inbox_dir, read_inbox_meta, load_manifest

    _park_review(temp_base_dir, doc_type="contract")
    mocker.patch(
        "graph.build_graph.resume_from_review",
        return_value={
            "stage": "archived",
            "doc_type": "insurance_claim",
            "extraction_confidence": 0.88,
            "extraction_attempts": 1,
        },
    )
    r = client.post(
        "/review/doc-review-1/resolve",
        headers=_auth(),
        json={
            "decision": "approved",
            "disposition": "resume",
            "doc_type": "insurance_claim",
            "doc_subclass": "pde",
            "notes": "sorter missed",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["class_override"]["doc_type"] == "insurance_claim"
    assert body["class_override"]["doc_subclass"] == "pde"
    m = load_manifest("doc-review-1")
    assert m.doc_type == "insurance_claim"
    assert m.doc_subclass == "pde"

    # Requeue stamps class override onto the inbox sidecar
    _park_review(temp_base_dir, doc_id="doc-rq", filename="claim.txt", doc_type="contract")
    rq = client.post(
        "/review/doc-rq/resolve",
        headers=_auth(),
        json={
            "decision": "rejected",
            "disposition": "requeue",
            "doc_type": "insurance_claim",
            "doc_subclass": "pde",
        },
    )
    assert rq.status_code == 200, rq.text
    inbox_name = rq.json()["inbox_file"]
    meta = read_inbox_meta(inbox_dir() / inbox_name)
    assert meta["doc_type"] == "insurance_claim"
    assert meta["doc_subclass"] == "pde"
    assert meta["note"] == "requeued_from_review"


def test_document_source_text_and_download(client, temp_base_dir):
    """GET /documents/{doc_id}/source — parked text pane + Open original."""
    _park_review(temp_base_dir)
    headers = _auth()
    r = client.get("/documents/doc-review-1/source", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["doc_id"] == "doc-review-1"
    assert body["filename"] == "parked.txt"
    assert "Acme" in body["text"]
    assert body["truncated"] is False
    assert body["readable"] is True
    assert body["bytes"] > 0

    dl = client.get(
        "/v1/documents/doc-review-1/source",
        params={"download": "1"},
        headers=headers,
    )
    assert dl.status_code == 200
    assert b"Acme" in dl.content
    assert "text" in (dl.headers.get("content-type") or "")

    missing = client.get("/documents/nope/source", headers=headers)
    assert missing.status_code == 404


def test_audit_analyze_endpoint_and_script(client, temp_base_dir):
    _park_review(temp_base_dir)
    # Seed one audit row via resolve record
    rec = client.post(
        "/review/doc-review-1/resolve",
        headers=_auth(),
        json={"decision": "approved", "disposition": "record", "notes": "n"},
    )
    assert rec.status_code == 200
    r = client.get("/audit", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["total_entries"] >= 1
    assert "by_event" in body
    assert "review_recorded" in body.get("review_events", {}) or "review_recorded" in body["by_event"]

    import asyncio
    from storage.audit_log import analyze_audit_db

    report = asyncio.run(analyze_audit_db(verify_chains=True, event_limit=5))
    assert report["total_entries"] >= 1


def test_v1_aliases_include_new_routes(client):
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/v1/lookup" in paths
    assert "/v1/review/queue" in paths
    assert "/v1/audit" in paths
    assert "/v1/documents/{doc_id}/source" in paths
