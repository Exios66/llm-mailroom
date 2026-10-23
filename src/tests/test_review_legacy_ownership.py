"""A sibling's owned copy cannot prove ownership of a leftover legacy file."""

import importlib

import pytest


def _manifest(doc_id, filename="same.txt"):
    from schemas.manifest import DocumentManifest, PipelineStage

    return DocumentManifest(doc_id=doc_id, matter_id=f"matter-{doc_id}",
                            original_filename=filename, stage=PipelineStage.REVIEW,
                            doc_type="contract")


def _legacy_with_sibling(temp_base_dir, *, sibling_owned=True):
    from pipeline.bins import park_for_review, review_dir, save_manifest

    requested, sibling = _manifest("REQUESTED"), _manifest("SIBLING")
    save_manifest(requested)
    save_manifest(sibling)
    legacy = review_dir() / "same.txt"
    legacy.write_bytes(b"SIBLING private legal document")
    owned = review_dir() / "same--SIBLING.txt"
    if sibling_owned:
        # A retained legacy copy and a later ID-qualified publication can
        # coexist: parking a replay does not remove the old bare-name copy.
        replay = temp_base_dir / "replay.txt"
        replay.write_bytes(legacy.read_bytes())
        assert park_for_review(replay, sibling) == (owned, True)
        assert owned.read_bytes() == b"SIBLING private legal document"
        assert not replay.exists()
    return requested, sibling, legacy, owned


@pytest.fixture
def client(temp_base_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from storage.db import ensure_schema
    import api.main

    monkeypatch.setenv("MAILROOM_API_TOKEN", "legacy-ownership-test-token")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    api = importlib.reload(api.main)
    ensure_schema()
    with TestClient(api.app, headers={
        "Authorization": "Bearer legacy-ownership-test-token",
    }) as result:
        yield result


@pytest.mark.parametrize("sibling_owned", [False, True])
@pytest.mark.parametrize("operation", ["resolve", "replay"])
def test_same_named_sibling_blocks_legacy(temp_base_dir, sibling_owned, operation):
    from pipeline.bins import park_for_review, review_dir, review_file_path
    from pipeline.review_resolve import locate_document_file

    manifest, _, legacy, sibling_path = _legacy_with_sibling(
        temp_base_dir, sibling_owned=sibling_owned,
    )
    if operation == "resolve":
        assert review_file_path(manifest) == review_dir() / "same--REQUESTED.txt"
        assert locate_document_file(manifest) is None
    else:
        with pytest.raises(FileNotFoundError, match="Cannot park for review"):
            park_for_review(temp_base_dir / "missing.txt", manifest)
    assert legacy.read_bytes() == b"SIBLING private legal document"
    if sibling_owned:
        assert sibling_path.read_bytes() == b"SIBLING private legal document"


@pytest.mark.parametrize("operation", ["text", "download", "requeue", "complete"])
def test_review_api_denies_ambiguous_legacy(temp_base_dir, client, operation):
    from pipeline.bins import archive_dir, inbox_dir, load_manifest

    _, _, legacy, sibling_path = _legacy_with_sibling(temp_base_dir)
    if operation in {"text", "download"}:
        response = client.get("/documents/REQUESTED/source",
                              params={"download": operation == "download"})
    else:
        response = client.post("/review/REQUESTED/resolve", json={
            "decision": "approved", "disposition": operation,
            "extracted_data": {"parties": ["Requested Party"], "confidence": 0.99},
        })
    assert response.status_code == 404, response.text
    assert "not found" in response.json()["detail"].lower()
    assert b"SIBLING private legal document" not in response.content
    assert legacy.read_bytes() == b"SIBLING private legal document"
    assert sibling_path.read_bytes() == b"SIBLING private legal document"
    assert load_manifest("REQUESTED").stage.value == "review"
    assert load_manifest("SIBLING").stage.value == "review"
    assert list(inbox_dir().iterdir()) == []
    assert not list(archive_dir().rglob("*.txt"))


@pytest.mark.parametrize("unrelated_sibling", [False, True])
def test_unique_legacy_fallback_remains_usable(temp_base_dir, client, unrelated_sibling):
    from pipeline.bins import (
        load_manifest, park_for_review, review_dir, review_file_path, save_manifest,
    )
    from pipeline.review_resolve import locate_document_file

    manifest = _manifest("REQUESTED")
    save_manifest(manifest)
    if unrelated_sibling:
        save_manifest(_manifest("UNRELATED", "different.txt"))
    legacy = review_dir() / "same.txt"
    legacy.write_bytes(b"REQUESTED unique legacy document")
    assert review_file_path(manifest) == legacy
    assert locate_document_file(manifest) == legacy
    assert park_for_review(temp_base_dir / "missing.txt", manifest) == (legacy, False)
    downloaded = client.get("/documents/REQUESTED/source?download=1")
    assert downloaded.status_code == 200
    assert downloaded.content == b"REQUESTED unique legacy document"
    completed = client.post("/review/REQUESTED/resolve", json={
        "decision": "approved", "disposition": "complete",
        "extracted_data": {"parties": ["Requested Party"], "confidence": 0.99},
    })
    assert completed.status_code == 200, completed.text
    assert completed.json()["complete"]["stage"] == "archived"
    assert load_manifest("REQUESTED").stage.value == "archived"
    assert not legacy.exists()
    archived = client.get("/documents/REQUESTED/source?download=1")
    assert archived.status_code == 200
    assert archived.content == b"REQUESTED unique legacy document"


def test_owned_file_takes_priority_over_ambiguous_legacy(temp_base_dir, client):
    from pipeline.bins import (
        load_manifest, park_for_review, review_file_path,
    )

    manifest, _, legacy, sibling_path = _legacy_with_sibling(temp_base_dir)
    source = temp_base_dir / "requested.txt"
    source.write_bytes(b"REQUESTED owned legal document")
    owned, newly = park_for_review(source, manifest)
    assert newly and not source.exists()
    assert owned.name == "same--REQUESTED.txt"
    assert review_file_path(manifest) == owned
    assert park_for_review(source, manifest) == (owned, False)
    downloaded = client.get("/documents/REQUESTED/source?download=1")
    assert downloaded.status_code == 200
    assert downloaded.content == b"REQUESTED owned legal document"
    completed = client.post("/review/REQUESTED/resolve", json={
        "decision": "approved", "disposition": "complete",
        "extracted_data": {"parties": ["Requested Party"], "confidence": 0.99},
    })
    assert completed.status_code == 200, completed.text
    assert completed.json()["complete"]["stage"] == "archived"
    assert load_manifest("REQUESTED").stage.value == "archived"
    assert not owned.exists()
    assert legacy.read_bytes() == b"SIBLING private legal document"
    assert sibling_path.read_bytes() == b"SIBLING private legal document"
    assert load_manifest("SIBLING").stage.value == "review"
