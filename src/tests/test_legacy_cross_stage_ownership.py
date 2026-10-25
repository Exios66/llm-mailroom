"""Current manifest metadata cannot prove ownership of stale legacy bytes."""

import importlib

import pytest


def _manifest(doc_id, stage="review", doc_type="contract"):
    from schemas.manifest import DocumentManifest

    return DocumentManifest(doc_id=doc_id, matter_id="M", original_filename="same.txt",
                            stage=stage, doc_type=doc_type)


def _legacy_path(kind):
    from pipeline import bins

    bases = {
        "review": bins.review_dir(),
        "failed": bins.failed_dir(),
        "archive": bins.archive_dir("M", "contract"),
        "processing": bins.processing_dir("worker"),
    }
    base = bases[kind]
    base.mkdir(parents=True, exist_ok=True)
    return base / "same.txt"


@pytest.fixture
def client(temp_base_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from storage.db import ensure_schema
    import api.main

    monkeypatch.setenv("MAILROOM_API_TOKEN", "cross-stage-ownership-test")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    monkeypatch.setenv("MAILROOM_WAREHOUSE_EXPORT", "0")
    api = importlib.reload(api.main)
    ensure_schema()
    with TestClient(api.app, headers={"Authorization": "Bearer cross-stage-ownership-test"}) as result:
        yield result


@pytest.mark.parametrize("kind", ["review", "failed", "archive", "processing"])
@pytest.mark.parametrize("peer_stage", ["inbox", "processing", "classified", "review", "failed", "archived"])
def test_all_stages_block_ambiguous_legacy_candidates(temp_base_dir, kind, peer_stage):
    from pipeline.bins import save_manifest
    from pipeline.review_resolve import locate_document_file

    requested = _manifest("REQUESTED", "archived" if kind == "archive" else kind)
    sibling = _manifest("SIBLING", peer_stage)
    save_manifest(requested)
    save_manifest(sibling)
    legacy = _legacy_path(kind)
    legacy.write_bytes(b"SIBLING private document")
    assert locate_document_file(requested) is None
    assert legacy.read_bytes() == b"SIBLING private document"
    owned = legacy.with_name("same--REQUESTED.txt")
    owned.write_bytes(b"REQUESTED verified document")
    assert locate_document_file(requested) == owned


@pytest.mark.parametrize("kind", ["review", "failed", "archive", "processing"])
def test_genuinely_unique_legacy_candidate_remains_usable(temp_base_dir, kind):
    from pipeline.bins import save_manifest
    from pipeline.review_resolve import locate_document_file

    requested = _manifest("REQUESTED", "failed")
    save_manifest(requested)
    legacy = _legacy_path(kind)
    legacy.write_bytes(b"unique private document")
    assert locate_document_file(requested) == legacy


@pytest.mark.parametrize("operation", ["text", "download", "requeue", "complete"])
def test_reject_move_failure_cannot_expose_stale_review_bytes(temp_base_dir, client, monkeypatch, operation):
    from pipeline import bins

    sibling = _manifest("SIBLING")
    bins.save_manifest(sibling)
    legacy = _legacy_path("review")
    legacy.write_bytes(b"SIBLING private document")
    real_move = bins.shutil.move
    attempts = []

    def fail_rejection_move(source, destination, *args, **kwargs):
        if str(source) == str(legacy):
            attempts.append((source, destination))
            raise PermissionError("injected filesystem move failure")
        return real_move(source, destination, *args, **kwargs)

    monkeypatch.setattr(bins.shutil, "move", fail_rejection_move)
    rejected = client.post("/review/SIBLING/resolve", json={"decision": "rejected"})
    assert rejected.status_code == 200, rejected.text
    assert len(attempts) == 1
    assert bins.load_manifest("SIBLING").stage.value == "failed"
    assert legacy.read_bytes() == b"SIBLING private document"

    requested = _manifest("REQUESTED")
    bins.save_manifest(requested)
    if operation in {"text", "download"}:
        response = client.get("/documents/REQUESTED/source", params={"download": operation == "download"})
    else:
        payload = {"decision": "approved", "disposition": operation}
        if operation == "complete":
            payload["extracted_data"] = {"parties": ["Requested Party"], "confidence": 0.99}
        response = client.post("/review/REQUESTED/resolve", json=payload)
    assert response.status_code == 404, response.text
    assert b"SIBLING private document" not in response.content
    assert bins.load_manifest("REQUESTED").stage.value == "review"
    assert bins.load_manifest("SIBLING").stage.value == "failed"
    assert legacy.read_bytes() == b"SIBLING private document"
    assert not list(bins.inbox_dir().iterdir())


def test_archive_class_override_does_not_authorize_stale_legacy_bytes(temp_base_dir, client):
    from pipeline import bins

    sibling = _manifest("SIBLING", "archived")
    bins.save_manifest(sibling)
    legacy = _legacy_path("archive")
    legacy.write_bytes(b"SIBLING private document")
    changed = client.post("/review/SIBLING/resolve", json={
        "decision": "approved", "disposition": "record", "override_doc_type": "correspondence",
    })
    assert changed.status_code == 200, changed.text
    assert bins.load_manifest("SIBLING").doc_type == "correspondence"
    assert legacy.read_bytes() == b"SIBLING private document"

    bins.save_manifest(_manifest("REQUESTED", "archived"))
    response = client.get("/documents/REQUESTED/source?download=1")
    assert response.status_code == 404, response.text
    assert b"SIBLING private document" not in response.content
    assert legacy.read_bytes() == b"SIBLING private document"
