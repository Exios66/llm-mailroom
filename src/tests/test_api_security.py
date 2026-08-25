"""API security tests (audit L-2/L-18): auth, upload guards, doc_id validation.

Covers the FastAPI app in api/main.py without touching the network:
- bearer-token enforcement on every endpoint except /health
- /upload size cap + rate limit + pause gate
- doc_id path validation
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def client(monkeypatch, tmp_path, temp_base_dir):
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


class TestAuth:
    def test_health_open_without_token(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_v1_open_without_token(self, client):
        r = client.get("/v1/health")
        assert r.status_code == 200
        assert r.json()["service"] == "mailroom"

    def test_upload_requires_token(self, client):
        r = client.post("/upload", files={"file": ("a.txt", b"hello", "text/plain")})
        assert r.status_code == 401

    def test_upload_accepted_with_token(self, client):
        r = client.post(
            "/upload",
            files={"file": ("a.txt", b"hello world", "text/plain")},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert r.status_code == 202

    def test_wrong_token_rejected(self, client):
        r = client.post(
            "/upload",
            files={"file": ("a.txt", b"hello", "text/plain")},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    def test_ops_endpoints_require_token(self, client):
        assert client.get("/ops/status").status_code == 401
        assert client.post("/ops/sweep").status_code == 401
        assert client.post("/ops/resume").status_code == 401
        assert client.get("/matters/X").status_code == 401
        assert client.get("/v1/ops/status").status_code == 401
        assert client.get("/v1/matters/X").status_code == 401

    def test_review_and_audit_require_token(self, client):
        assert client.get("/audit/anything").status_code == 401
        assert client.get("/status/anything").status_code == 401
        assert client.post("/review/x/resolve", data={"decision": "approved"}).status_code == 401


class TestUploadGuards:
    def test_size_cap_rejected(self, client):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("api.main.MAX_UPLOAD_BYTES", 16)
        big = b"x" * 32
        r = client.post(
            "/upload",
            files={"file": ("big.txt", big, "text/plain")},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert r.status_code == 413
        monkeypatch.undo()

    def test_pause_gate_503(self, client, monkeypatch):
        monkeypatch.setattr("pipeline.bins.is_ingestion_paused", lambda: True)
        r = client.post(
            "/upload",
            files={"file": ("a.txt", b"hello", "text/plain")},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert r.status_code == 503

    def test_unsupported_extension_400(self, client):
        r = client.post(
            "/upload",
            files={"file": ("a.exe", b"MZ", "application/octet-stream")},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert r.status_code == 400

    def test_rate_limit_429(self, client, monkeypatch):
        monkeypatch.setattr("api.main._UPLOAD_MAX_PER_WINDOW", 2)
        headers = {"Authorization": "Bearer test-token-123"}
        assert client.post("/upload", files={"file": ("a.txt", b"1", "text/plain")}, headers=headers).status_code == 202
        assert client.post("/upload", files={"file": ("b.txt", b"2", "text/plain")}, headers=headers).status_code == 202
        assert client.post("/upload", files={"file": ("c.txt", b"3", "text/plain")}, headers=headers).status_code == 429


class TestDocIdValidation:
    def test_bad_doc_id_rejected(self, client):
        headers = {"Authorization": "Bearer test-token-123"}
        # Encoded space must not reach manifest paths (L-21). Encoded slashes
        # are already rejected by Starlette routing (404) — defense in depth.
        assert client.get("/status/a%20b", headers=headers).status_code == 400
        assert client.get("/audit/a%20b", headers=headers).status_code == 400
        r = client.post("/review/a%20b/resolve", data={"decision": "approved"}, headers=headers)
        assert r.status_code == 400

    def test_valid_doc_id_passes_validation(self, client):
        headers = {"Authorization": "Bearer test-token-123"}
        # Validation passes; 404 (manifest missing) proves the id was accepted.
        assert client.get("/status/ok_doc-1", headers=headers).status_code == 404
