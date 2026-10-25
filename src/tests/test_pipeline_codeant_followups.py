"""DMR-111: real file and Parquet regressions for the follow-up review."""
from __future__ import annotations

import errno
import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest


@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("operation", ["claim", "resume"])
def test_reused_processing_worker_preserves_both_documents(temp_base_dir, monkeypatch, cross_device, operation):
    from pipeline import bins
    incoming = bins.inbox_dir() / "same.txt"
    incoming.write_bytes(b"NEW private document")
    old = bins.processing_dir("reused") / incoming.name
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"OLD private document")
    if cross_device:
        real_link = bins.os.link
        def link(src, dest, *args, **kwargs):
            if Path(src) == incoming:
                raise OSError(errno.EXDEV, "different filesystems")
            return real_link(src, dest, *args, **kwargs)
        monkeypatch.setattr(bins.os, "link", link)
    with pytest.raises(FileExistsError):
        if operation == "claim":
            bins.claim_file(incoming, "reused")
        else:
            bins.requeue_from_review(incoming, "reused", original_filename="same.txt")
    assert old.read_bytes() == b"OLD private document"
    assert incoming.read_bytes() == b"NEW private document"
    assert list(old.parent.iterdir()) == [old]


@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("operation", ["claim", "resume"])
def test_processing_claim_succeeds_without_collision(temp_base_dir, monkeypatch, cross_device, operation):
    from pipeline import bins
    incoming = bins.inbox_dir() / "same.txt"
    incoming.write_bytes(b"NEW private document")
    if cross_device:
        real_link = bins.os.link
        def link(src, dest, *args, **kwargs):
            if Path(src) == incoming:
                raise OSError(errno.EXDEV, "different filesystems")
            return real_link(src, dest, *args, **kwargs)
        monkeypatch.setattr(bins.os, "link", link)
    claimed = (bins.claim_file(incoming, "reused") if operation == "claim" else
               bins.requeue_from_review(incoming, "reused", original_filename="same.txt"))
    assert claimed == bins.processing_dir("reused") / "same.txt"
    assert claimed.read_bytes() == b"NEW private document"
    assert not incoming.exists()
    assert list(claimed.parent.iterdir()) == [claimed]


def test_simultaneous_processing_claims_publish_exactly_one(temp_base_dir):
    from pipeline import bins
    sources = []
    for index in range(2):
        source = temp_base_dir / f"incoming-{index}" / "same.txt"
        source.parent.mkdir()
        source.write_bytes(f"private document {index}".encode())
        sources.append(source)
    barrier = Barrier(2)
    def claim(source):
        barrier.wait(timeout=5)
        try:
            bins.claim_file(source, "shared-worker")
            return "ok"
        except FileExistsError:
            return "collision"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, sources))
    assert sorted(results) == ["collision", "ok"]
    winner = results.index("ok")
    dest = bins.processing_dir("shared-worker") / "same.txt"
    assert dest.read_bytes() == f"private document {winner}".encode()
    assert not sources[winner].exists()
    assert sources[1-winner].read_bytes() == f"private document {1-winner}".encode()


@pytest.mark.parametrize("old_stamp", ["2026-10-25T01:00:00+00:00", "2026-10-25T03:00:00+02:00"])
def test_delayed_older_export_cannot_replace_newer_document(temp_base_dir, monkeypatch, old_stamp):
    from storage import warehouse
    monkeypatch.setenv("MAILROOM_WAREHOUSE_EXPORT", "1")
    stamp = date(2026, 10, 25)
    new_stamp = "2026-10-25T02:00:00+00:00"
    # Each batch represents rows already loaded before the publication lock.
    newer = {"doc_id": "SAME", "stage": "archived", "updated_at": new_stamp,
             "extracted_data_json": '{"parties": ["NEW"]}'}
    older = {"doc_id": "SAME", "stage": "failed", "updated_at": old_stamp,
             "extracted_data_json": '{"parties": ["OLD"]}'}
    warehouse._publish_export(stamp, [newer], [], datetime.fromisoformat(new_stamp))
    warehouse._publish_export(stamp, [older], [], datetime.fromisoformat(old_stamp))
    rows = warehouse._read_parquet_rows(warehouse.daily_documents_path(stamp))
    assert rows == [newer]
    assert warehouse.load_warehouse_manifest()["last_doc_updated_at"] == new_stamp
    newer["updated_at"] = "2026-10-25T04:00:00+00:00"
    newer["extracted_data_json"] = '{"parties": ["LATEST"]}'
    warehouse._publish_export(stamp, [newer], [], datetime.fromisoformat(newer["updated_at"]))
    assert warehouse._read_parquet_rows(warehouse.daily_documents_path(stamp)) == [newer]


def test_timestamp_guard_does_not_change_generic_or_audit_upserts():
    from storage.warehouse import _merge_rows
    assert _merge_rows([{"doc_id": "D", "value": 1}], [{"doc_id": "D", "value": 2}], "doc_id") == [{"doc_id": "D", "value": 2}]
    assert _merge_rows([{"entry_id": "E", "value": 1}], [{"entry_id": "E", "value": 2}], "entry_id") == [{"entry_id": "E", "value": 2}]


def _failed_pair():
    from pipeline.bins import failed_dir, save_manifest
    from schemas.manifest import DocumentManifest, PipelineStage
    requested = DocumentManifest(doc_id="REQUESTED", matter_id="matter-A", original_filename="same.txt",
                                 stage=PipelineStage.FAILED, doc_type="contract")
    sibling = requested.model_copy(update={"doc_id": "SIBLING", "matter_id": "matter-B"})
    save_manifest(requested)
    save_manifest(sibling)
    legacy = failed_dir() / "same.txt"
    legacy.write_bytes(b"SIBLING confidential document")
    return requested, sibling, legacy


@pytest.mark.parametrize("sibling_owned", [False, True])
def test_ambiguous_failed_basename_has_no_owner(temp_base_dir, sibling_owned):
    from pipeline.bins import failed_dir
    from pipeline.review_resolve import locate_document_file
    requested, sibling, legacy = _failed_pair()
    if sibling_owned:
        (failed_dir() / "same--SIBLING.txt").write_bytes(legacy.read_bytes())
    assert locate_document_file(requested) is None
    assert legacy.read_bytes() == b"SIBLING confidential document"
    owned = failed_dir() / "same--REQUESTED.txt"
    owned.write_bytes(b"REQUESTED verified document")
    assert locate_document_file(requested) == owned


@pytest.mark.parametrize("broken", ["{", "[]", '{"doc_id": "../unsafe"}'])
def test_unverifiable_failed_legacy_fails_closed(temp_base_dir, broken):
    from pipeline.bins import manifests_dir
    from pipeline.review_resolve import locate_document_file
    requested, _, legacy = _failed_pair()
    (manifests_dir() / "SIBLING.json").write_text(broken)
    assert locate_document_file(requested) is None
    assert legacy.read_bytes() == b"SIBLING confidential document"


def test_unique_legacy_failed_file_stays_available(temp_base_dir):
    from pipeline.bins import manifests_dir
    from pipeline.review_resolve import locate_document_file
    requested, _, legacy = _failed_pair()
    (manifests_dir() / "SIBLING.json").unlink()
    assert locate_document_file(requested) == legacy


@pytest.mark.parametrize("operation", ["text", "download", "requeue"])
def test_api_cannot_expose_or_requeue_another_failed_document(temp_base_dir, monkeypatch, operation):
    from fastapi.testclient import TestClient
    from pipeline.bins import inbox_dir, load_manifest
    from storage.db import ensure_schema
    import api.main
    monkeypatch.setenv("MAILROOM_API_TOKEN", "failed-ownership-test")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    api = importlib.reload(api.main)
    ensure_schema()
    requested, _, legacy = _failed_pair()
    with TestClient(api.app, headers={"Authorization": "Bearer failed-ownership-test"}) as client:
        if operation == "requeue":
            response = client.post("/review/REQUESTED/resolve", json={"decision": "approved", "disposition": "requeue"})
        else:
            response = client.get("/documents/REQUESTED/source", params={"download": operation == "download"})
    assert response.status_code == 404, response.text
    assert b"SIBLING confidential document" not in response.content
    assert legacy.read_bytes() == b"SIBLING confidential document"
    assert load_manifest(requested.doc_id).stage.value == "failed"
    assert not list(inbox_dir().iterdir())


def _legacy_candidate(kind):
    from pipeline import bins
    from schemas.manifest import DocumentManifest, PipelineStage

    stages = {"archive": PipelineStage.ARCHIVED, "processing": PipelineStage.PROCESSING}
    requested = DocumentManifest(doc_id="REQUESTED", matter_id="M", original_filename="same.txt",
                                 stage=stages[kind], doc_type="contract")
    sibling = requested.model_copy(update={"doc_id": "SIBLING"})
    bins.save_manifest(requested)
    bins.save_manifest(sibling)
    base = bins.archive_dir("M", "contract") if kind == "archive" else bins.processing_dir("worker")
    base.mkdir(parents=True, exist_ok=True)
    legacy = base / "same.txt"
    legacy.write_bytes(b"SIBLING confidential document")
    return requested, sibling, legacy


@pytest.mark.parametrize("kind", ["archive", "processing"])
@pytest.mark.parametrize("sibling_owned", [False, True])
def test_every_legacy_locator_candidate_requires_unambiguous_ownership(temp_base_dir, kind, sibling_owned):
    from pipeline.review_resolve import locate_document_file

    requested, _, legacy = _legacy_candidate(kind)
    if sibling_owned:
        (legacy.parent / "same--SIBLING.txt").write_bytes(legacy.read_bytes())
    assert locate_document_file(requested) is None
    owned = legacy.parent / "same--REQUESTED.txt"
    owned.write_bytes(b"REQUESTED verified document")
    assert locate_document_file(requested) == owned
    assert legacy.read_bytes() == b"SIBLING confidential document"


@pytest.mark.parametrize("kind", ["archive", "processing"])
@pytest.mark.parametrize("broken", ["{", "[]", '{"doc_id": "../unsafe"}'])
def test_every_legacy_locator_candidate_rejects_unverifiable_ownership(temp_base_dir, kind, broken):
    from pipeline.bins import manifests_dir
    from pipeline.review_resolve import locate_document_file

    requested, _, legacy = _legacy_candidate(kind)
    (manifests_dir() / "SIBLING.json").write_text(broken)
    assert locate_document_file(requested) is None
    assert legacy.read_bytes() == b"SIBLING confidential document"


@pytest.mark.parametrize("kind", ["archive", "processing"])
def test_unique_legacy_archive_and_processing_files_stay_available(temp_base_dir, kind):
    from pipeline.bins import manifests_dir
    from pipeline.review_resolve import locate_document_file

    requested, _, legacy = _legacy_candidate(kind)
    (manifests_dir() / "SIBLING.json").unlink()
    assert locate_document_file(requested) == legacy


@pytest.mark.parametrize("other_scope", [{"matter_id": "OTHER"}, {"doc_type": "correspondence"}])
def test_archive_current_metadata_cannot_prove_legacy_ownership(temp_base_dir, other_scope):
    from pipeline.bins import save_manifest
    from pipeline.review_resolve import locate_document_file

    requested, sibling, legacy = _legacy_candidate("archive")
    save_manifest(sibling.model_copy(update=other_scope))
    assert locate_document_file(requested) is None
    assert legacy.read_bytes() == b"SIBLING confidential document"


def test_processing_legacy_cannot_choose_between_workers(temp_base_dir):
    from pipeline.bins import manifests_dir, processing_dir
    from pipeline.review_resolve import locate_document_file

    requested, _, legacy = _legacy_candidate("processing")
    (manifests_dir() / "SIBLING.json").unlink()
    other = processing_dir("other-worker") / "same.txt"
    other.parent.mkdir()
    other.write_bytes(b"OTHER confidential document")
    assert locate_document_file(requested) is None
    assert legacy.exists() and other.exists()


def test_processing_classified_sibling_blocks_legacy(temp_base_dir):
    from pipeline.bins import save_manifest
    from pipeline.review_resolve import locate_document_file
    from schemas.manifest import PipelineStage

    requested, sibling, legacy = _legacy_candidate("processing")
    save_manifest(sibling.model_copy(update={"stage": PipelineStage.CLASSIFIED}))
    assert locate_document_file(requested) is None
    assert legacy.exists()


@pytest.mark.parametrize("kind", ["archive", "processing"])
@pytest.mark.parametrize("operation", ["text", "download", "requeue"])
def test_api_cannot_expose_or_requeue_other_legacy_candidates(temp_base_dir, monkeypatch, kind, operation):
    from fastapi.testclient import TestClient
    from pipeline.bins import inbox_dir, load_manifest
    from storage.db import ensure_schema
    import api.main

    monkeypatch.setenv("MAILROOM_API_TOKEN", "candidate-ownership-test")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    api = importlib.reload(api.main)
    ensure_schema()
    requested, _, legacy = _legacy_candidate(kind)
    original_stage = requested.stage
    with TestClient(api.app, headers={"Authorization": "Bearer candidate-ownership-test"}) as client:
        if operation == "requeue":
            response = client.post("/review/REQUESTED/resolve", json={"decision": "approved", "disposition": "requeue"})
        else:
            response = client.get("/documents/REQUESTED/source", params={"download": operation == "download"})
    assert response.status_code == 404, response.text
    assert b"SIBLING confidential document" not in response.content
    assert load_manifest(requested.doc_id).stage == original_stage
    assert legacy.read_bytes() == b"SIBLING confidential document"
    assert not list(inbox_dir().iterdir())


@pytest.mark.parametrize("missing", [None, "absent"])
def test_unversioned_incoming_row_cannot_replace_known_catalog_revision(temp_base_dir, missing):
    from storage import warehouse

    path = warehouse.daily_documents_path(date(2026, 10, 25))
    current = {"doc_id": "D", "updated_at": "2026-10-25T01:00:00+00:00", "value": "current"}
    incoming = {"doc_id": "D", "value": "unknown"}
    if missing is None:
        incoming["updated_at"] = None
    warehouse.write_parquet_rows(path, [current], merge_key="doc_id")
    warehouse.write_parquet_rows(path, [incoming], merge_key="doc_id")
    assert warehouse._read_parquet_rows(path) == [current]


@pytest.mark.parametrize("bad", ["not-a-time", "", "2026-99-01"])
@pytest.mark.parametrize("side", ["existing", "incoming"])
def test_malformed_catalog_revision_aborts_without_replacing_parquet(temp_base_dir, bad, side):
    from storage import warehouse

    path = warehouse.daily_documents_path(date(2026, 10, 25))
    current = {"doc_id": "D", "updated_at": bad if side == "existing" else "2026-10-25T01:00:00+00:00"}
    incoming = {"doc_id": "D", "updated_at": bad if side == "incoming" else None}
    # Seed real corrupt historical bytes without using the validating writer.
    pa, pq = warehouse._require_pyarrow()
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([current]), path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        warehouse.write_parquet_rows(path, [incoming], merge_key="doc_id")
    assert path.read_bytes() == before
