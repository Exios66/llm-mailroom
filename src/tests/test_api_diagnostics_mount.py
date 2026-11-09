"""The producer API's diagnostics mount is opt-in and failure-tolerant."""

from __future__ import annotations

import builtins

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_mount_is_disabled_by_default(monkeypatch):
    from api import main

    monkeypatch.delenv("MAILROOM_DIAGNOSTICS_MOUNT", raising=False)
    probe = FastAPI()
    assert main._mount_diagnostics(probe) is False
    paths = {getattr(route, "path", "") for route in probe.routes}
    assert not any(path.startswith("/v1/diagnostics") for path in paths)


def test_mount_serves_read_only_routes_when_enabled(monkeypatch):
    from api import main

    monkeypatch.setenv("MAILROOM_DIAGNOSTICS_MOUNT", "1")
    monkeypatch.setenv("MAILROOM_API_TOKEN", "mount-token")
    probe = FastAPI()
    assert main._mount_diagnostics(probe) is True
    client = TestClient(probe)
    headers = {"Authorization": "Bearer mount-token"}
    assert client.get("/v1/diagnostics/capabilities", headers=headers).status_code == 200
    assert client.get("/v1/diagnostics/snapshot", headers=headers).status_code == 200
    assert client.get("/v1/diagnostics/findings", headers=headers).status_code == 200
    assert client.get("/diagnostics", headers=headers).status_code == 200
    assert client.get("/v1/diagnostics/capabilities").status_code == 401


def test_missing_diagnostics_package_never_breaks_api_startup(monkeypatch):
    from api import main

    monkeypatch.setenv("MAILROOM_DIAGNOSTICS_MOUNT", "1")
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "mailroom_diagnostics" or name.startswith("mailroom_diagnostics."):
            raise ImportError("diagnostics package is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    probe = FastAPI()
    assert main._mount_diagnostics(probe) is False
    assert not any(
        getattr(route, "path", "").startswith("/v1/diagnostics") for route in probe.routes
    )
