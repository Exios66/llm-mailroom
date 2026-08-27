"""API-embedded inbox watcher (The-Mailroom live-floor producer contract)."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture
def reload_api(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    for mod in ("api.main",):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from api.main import app

    return app


def test_embed_watcher_default_off_under_pytest():
    from pipeline.watcher import embed_watcher_enabled

    assert embed_watcher_enabled() is False


def test_embed_watcher_env_on(monkeypatch):
    from pipeline.watcher import embed_watcher_enabled

    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "1")
    assert embed_watcher_enabled() is True
    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "0")
    assert embed_watcher_enabled() is False


def test_api_lifespan_starts_and_stops_watcher(monkeypatch, reload_api):
    started = []

    class FakeWatcher:
        worker_id = "test-worker"
        _running = False

        def start(self):
            self._running = True
            started.append("start")

        def stop(self):
            self._running = False
            started.append("stop")

    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "1")
    monkeypatch.setattr("pipeline.watcher.Watcher", FakeWatcher)
    app = reload_api
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["checks"]["watcher_embedded"] is True
        assert started == ["start"]
    assert started == ["start", "stop"]


def test_api_lifespan_skips_when_lock_held(monkeypatch, reload_api):
    from pipeline.watcher import WatcherLockHeld

    class LockedWatcher:
        worker_id = "test-worker"
        _running = False

        def start(self):
            raise WatcherLockHeld("held")

        def stop(self):
            raise AssertionError("stop should not run when start failed")

    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "1")
    monkeypatch.setattr("pipeline.watcher.Watcher", LockedWatcher)
    app = reload_api
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["checks"]["watcher_embedded"] is False


def test_second_start_raises_in_process(temp_base_dir, monkeypatch):
    from pipeline.watcher import Watcher, WatcherLockHeld

    monkeypatch.setattr("pipeline.watcher.Observer", MagicMock)
    w1 = Watcher()
    w1.observer = MagicMock()
    w1.start()
    try:
        w2 = Watcher()
        w2.observer = MagicMock()
        with pytest.raises(WatcherLockHeld):
            w2.start()
    finally:
        w1.stop()
