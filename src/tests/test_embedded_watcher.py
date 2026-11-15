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


def test_api_lifespan_refuses_to_start_when_a_specialist_pin_is_broken(
    monkeypatch, reload_api, broken_specialist_pin
):
    """The API must not come up (and accept uploads) on a Dojo pin that cannot
    serve the frozen specialist prompts, whether or not the watcher is embedded."""
    started = []

    class FakeWatcher:
        worker_id = "test-worker"
        _running = False

        def start(self):
            started.append("start")

        def stop(self):
            started.append("stop")

    monkeypatch.setenv("MAILROOM_EMBED_WATCHER", "1")
    monkeypatch.setattr("pipeline.watcher.Watcher", FakeWatcher)
    with pytest.raises(broken_specialist_pin.SpecialistPinError, match="corporate_records_specialist"):
        with TestClient(reload_api):
            pass
    assert started == []  # the check runs before any watcher is started


def test_api_lifespan_refuses_a_broken_pin_without_an_embedded_watcher(
    reload_api, broken_specialist_pin
):
    from pipeline.watcher import embed_watcher_enabled

    assert embed_watcher_enabled() is False
    with pytest.raises(broken_specialist_pin.SpecialistPinError):
        with TestClient(reload_api):
            pass


def test_watcher_start_refuses_a_broken_specialist_pin(
    temp_base_dir, monkeypatch, broken_specialist_pin
):
    """No lock, observer, inbox scan or worker thread before the pin check passes."""
    import pipeline.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "Observer", MagicMock)
    w = watcher_mod.Watcher()
    w.observer = MagicMock()
    try:
        with pytest.raises(broken_specialist_pin.SpecialistPinError, match="corporate_records_specialist"):
            w.start()
        assert w._lock is None
        assert w._running is False
        assert watcher_mod._watcher_owned is False
        w.observer.schedule.assert_not_called()
        w.observer.start.assert_not_called()
    finally:
        if w._running or w._lock is not None:  # only when the guard is missing
            w.stop()


def test_watcher_and_api_start_with_the_real_pins(temp_base_dir, monkeypatch, reload_api):
    """Control: the same paths start normally when the pins resolve."""
    import pipeline.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "Observer", MagicMock)
    w = watcher_mod.Watcher()
    w.observer = MagicMock()
    w.start()
    try:
        assert w._running is True
    finally:
        w.stop()
    with TestClient(reload_api) as client:
        assert client.get("/health").status_code == 200
