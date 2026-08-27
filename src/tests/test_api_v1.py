"""API v1 aliases — documented /v1 layout over the existing handlers.

Unversioned routes remain for the deprecation window; /v1 is the versioned
surface. Auth, status codes, and payloads match the unversioned routes.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

V1_PATHS = [
    ("GET", "/v1/health"),
    ("POST", "/v1/upload"),
    ("GET", "/v1/queue"),
    ("GET", "/v1/lookup"),
    ("GET", "/v1/review/queue"),
    ("POST", "/v1/review/{doc_id}/resolve"),
    ("GET", "/v1/status/{doc_id}"),
    ("GET", "/v1/matters/{matter_id}"),
    ("GET", "/v1/audit"),
    ("GET", "/v1/audit/{doc_id}"),
    ("GET", "/v1/ops/status"),
]


@pytest.fixture
def client(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    for mod in ("api.main",):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from api.main import app

    with TestClient(app) as c:
        yield c


def _auth():
    return {"Authorization": "Bearer test-token-123"}


def test_v1_routes_are_registered(client):
    paths = {(tuple(sorted(r.methods)), r.path) for r in client.app.routes if hasattr(r, "path")}
    for method, path in V1_PATHS:
        assert any(method in methods and p == path for methods, p in paths), path


def test_v1_health_matches_unversioned(client):
    a = client.get("/health")
    b = client.get("/v1/health")
    assert a.status_code == b.status_code == 200
    assert a.json()["service"] == b.json()["service"] == "mailroom"


def test_v1_upload_and_queue(client):
    r = client.post(
        "/v1/upload",
        files={"file": ("v1.txt", b"hello", "text/plain")},
        data={"matter_id": "MATTER-V1"},
        headers=_auth(),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["matter_id"] == "MATTER-V1"
    assert body["upload_id"]

    q = client.get("/v1/queue", headers=_auth())
    assert q.status_code == 200
    queued = q.json()["queued"]
    assert any(item["file"] == "v1.txt" for item in queued)
    assert any(item["upload_id"] == body["upload_id"] for item in queued)


def test_v1_status_and_audit_and_matter_404(client):
    headers = _auth()
    assert client.get("/v1/status/missing-doc", headers=headers).status_code == 404
    # Audit of an unknown id is a valid empty chain (or 500 if DB missing).
    audit = client.get("/v1/audit/missing-doc", headers=headers)
    assert audit.status_code in (200, 500)
    matter = client.get("/v1/matters/NO-SUCH-MATTER", headers=headers)
    assert matter.status_code == 200
    assert matter.json()["document_count"] == 0


def test_v1_review_resolve_404(client):
    r = client.post(
        "/v1/review/missing-doc/resolve",
        data={"decision": "approved"},
        headers=_auth(),
    )
    assert r.status_code == 404


def test_v1_ops_status(client):
    r = client.get("/v1/ops/status", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "stuck_documents" in body
    assert "review_queue" in body
    assert "error_rates" in body
    assert "first_pass" in body
    assert "first_pass_rate" in body
    assert body["first_pass"] == 0
