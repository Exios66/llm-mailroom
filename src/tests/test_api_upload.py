"""Live-upload → pipeline queue tests.

Covers the `/upload` → inbox → watcher conveyor:
- `/upload` writes the file + an upload-metadata `.meta` sidecar carrying the
  submitted matter_id and a tracking upload_id, and returns them.
- `GET /queue` lists queued files with their upload metadata.
- The watcher's matter inference honors the sidecar before filename heuristics.
- The watcher's extension filter means `.meta` sidecars are never claimed.
- The watcher heartbeat (liveness for /health) is written and readable.
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def client(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    # Re-import so module-level token/env are fresh.
    for mod in ("api.main",):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from api.main import app

    with TestClient(app) as c:
        yield c


def _auth():
    return {"Authorization": "Bearer test-token-123"}


class TestUploadMetadata:
    def test_upload_writes_file_and_meta_sidecar(self, client):
        r = client.post(
            "/upload",
            files={"file": ("agreement.txt", b"hello", "text/plain")},
            data={"matter_id": "MATTER-001"},
            headers=_auth(),
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "accepted"
        assert body["matter_id"] == "MATTER-001"
        assert body["upload_id"]

        from pipeline.bins import inbox_dir

        inbox = inbox_dir()
        assert (inbox / "agreement.txt").exists()
        assert (inbox / "agreement.txt.meta").exists()
        meta = json.loads((inbox / "agreement.txt.meta").read_text())
        assert meta["matter_id"] == "MATTER-001"
        assert meta["upload_id"] == body["upload_id"]
        assert meta["size"] == 5
        assert meta["original_filename"] == "agreement.txt"
        assert meta["uploaded_at"]

    def test_upload_default_matter_id(self, client):
        r = client.post(
            "/upload",
            files={"file": ("a.txt", b"x", "text/plain")},
            headers=_auth(),
        )
        assert r.status_code == 202
        assert r.json()["matter_id"] == "DEFAULT"

    def test_upload_collision_uniquifies_name(self, client):
        headers = _auth()
        assert client.post(
            "/upload", files={"file": ("dup.txt", b"1", "text/plain")}, headers=headers
        ).status_code == 202
        r = client.post(
            "/upload", files={"file": ("dup.txt", b"2", "text/plain")}, headers=headers
        )
        assert r.status_code == 202
        assert r.json()["file"] == "dup-1.txt"


class TestQueue:
    def test_queue_lists_uploaded_file(self, client):
        headers = _auth()
        client.post(
            "/upload",
            files={"file": ("contract.txt", b"hi", "text/plain")},
            data={"matter_id": "M1"},
            headers=headers,
        )
        r = client.get("/queue", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["queued_count"] == 1
        queued = body["queued"][0]
        assert queued["file"] == "contract.txt"
        assert queued["matter_id"] == "M1"
        assert queued["upload_id"]

    def test_queue_requires_token(self, client):
        assert client.get("/queue").status_code == 401

    def test_queue_empty_when_inbox_empty(self, client):
        r = client.get("/queue", headers=_auth())
        assert r.status_code == 200
        assert r.json()["queued_count"] == 0


class TestWatcherMatterInference:
    def test_sidecar_matter_id_wins_over_filename(self, temp_base_dir):
        from pipeline import watcher
        from pipeline.bins import inbox_dir, write_inbox_meta

        f = inbox_dir() / "contract_FINAL.txt"  # filename would infer "FINAL"
        f.write_bytes(b"x")
        write_inbox_meta(f, matter_id="MATTER-001", upload_id="abc123")

        handler = watcher.InboxHandler("worker")
        assert handler._infer_matter_id(f) == "MATTER-001"

    def test_no_sidecar_falls_back_to_filename(self, temp_base_dir):
        from pipeline import watcher
        from pipeline.bins import inbox_dir

        f = inbox_dir() / "contract_FINAL.txt"
        f.write_bytes(b"x")
        handler = watcher.InboxHandler("worker")
        assert handler._infer_matter_id(f) == "FINAL"

    def test_no_sidecar_default_matter(self, temp_base_dir):
        from pipeline import watcher
        from pipeline.bins import inbox_dir

        f = inbox_dir() / "plain.txt"
        f.write_bytes(b"x")
        handler = watcher.InboxHandler("worker")
        assert handler._infer_matter_id(f) == "DEFAULT"


class TestWatcherExtensionFilter:
    def test_meta_sidecar_not_processable(self, temp_base_dir):
        from pipeline import watcher
        from pipeline.bins import inbox_dir

        handler = watcher.InboxHandler("worker")
        assert handler._is_processable(inbox_dir() / "contract.pdf.meta") is False

    def test_accepted_extensions_processable(self, temp_base_dir):
        from pipeline import watcher
        from pipeline.bins import inbox_dir

        handler = watcher.InboxHandler("worker")
        for name in ("a.txt", "a.pdf", "a.docx", "a.md"):
            assert handler._is_processable(inbox_dir() / name) is True
        assert handler._is_processable(inbox_dir() / "a.exe") is False


class TestWatcherHeartbeat:
    def test_heartbeat_touch_and_age(self, temp_base_dir):
        from pipeline import bins

        assert bins.watcher_heartbeat_age() is None  # no heartbeat yet
        bins.touch_watcher_heartbeat()
        age = bins.watcher_heartbeat_age()
        assert age is not None and 0 <= age < 10
