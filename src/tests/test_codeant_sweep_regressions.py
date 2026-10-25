"""DMR-113: CodeAnt findings that survived the DMR-111/112 review rounds."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_processing_listing_oserror_does_not_abort_other_bin_lookup(
    temp_base_dir, monkeypatch
):
    """A transient processing-directory error must not turn source lookup into 500.

    ``locate_document_file`` already guards each candidate ``is_file`` call.
    The processing-root listing happens while building the candidate list, so
    it needs the same best-effort treatment.
    """
    from pipeline import bins
    from pipeline.review_resolve import locate_document_file
    from schemas.manifest import DocumentManifest, PipelineStage

    manifest = DocumentManifest(
        doc_id="VISIBLE",
        matter_id="M",
        original_filename="same.txt",
        stage=PipelineStage.REVIEW,
        doc_type="contract",
    )
    bins.save_manifest(manifest)
    owned = bins.review_dir() / "same--VISIBLE.txt"
    owned.write_bytes(b"review bytes")
    real_iterdir = Path.iterdir

    def failing_iterdir(self):
        if self == bins.processing_dir():
            raise PermissionError("processing root unreadable")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    assert locate_document_file(manifest) == owned


def test_rescan_reconciles_stale_claims_periodically(temp_base_dir, monkeypatch):
    """A dead worker's stranded claim must not wait for a watcher restart."""
    import time
    from pipeline import watcher as watcher_mod

    watcher = watcher_mod.Watcher.__new__(watcher_mod.Watcher)
    watcher._running = True
    calls = []
    watcher._reconcile_stale_claims = lambda: calls.append("reconcile")

    clock = iter([0.0, 0.0, 61.0, 61.0])
    sleeps = []

    def fake_sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) >= 2:
            watcher._running = False

    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(watcher_mod, "touch_watcher_heartbeat", lambda **kwargs: None)
    monkeypatch.setattr(watcher_mod, "_get_manifest_index", lambda: {})
    monkeypatch.setattr(watcher_mod, "is_ingestion_paused", lambda: False)
    monkeypatch.setattr(watcher_mod, "list_inbox_files", lambda: [])

    watcher._rescan_loop()
    assert calls == ["reconcile"]


def test_requeue_after_doc_type_override_finds_old_archive_path(
    temp_base_dir, monkeypatch
):
    """An archived file remains requeueable after a classification override.

    The override is persisted before the requeue lookup; the old archive path
    is keyed by the pre-override doc_type and must still be searched.
    """
    from fastapi.testclient import TestClient
    from pipeline import bins
    from schemas.manifest import DocumentManifest, PipelineStage
    from storage.db import ensure_schema
    import api.main

    monkeypatch.setenv("MAILROOM_API_TOKEN", "codeant-requeue-test")
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    api = importlib.reload(api.main)
    ensure_schema()

    manifest = DocumentManifest(
        doc_id="ARCHIVED",
        matter_id="M",
        original_filename="same.txt",
        stage=PipelineStage.ARCHIVED,
        doc_type="contract",
    )
    bins.save_manifest(manifest)
    archive = bins.archive_dir("M", "contract") / "same--ARCHIVED.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archived bytes")

    with TestClient(
        api.app, headers={"Authorization": "Bearer codeant-requeue-test"}
    ) as client:
        response = client.post(
            "/review/ARCHIVED/resolve",
            json={
                "decision": "approved",
                "disposition": "requeue",
                "override_doc_type": "correspondence",
            },
        )

    assert response.status_code == 200, response.text
    assert archive.read_bytes() == b"archived bytes"
    inbox = bins.list_inbox_files()
    assert len(inbox) == 1
    assert inbox[0].read_bytes() == b"archived bytes"
    assert bins.load_manifest("ARCHIVED").doc_type == "correspondence"
