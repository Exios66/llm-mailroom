"""Asset modes: built UI served under /diagnostics, source/missing fallback."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_headers(monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "asset-token")
    return {"Authorization": "Bearer asset-token"}


def _built_tree(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>diagnostics-ui</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('diag')\n", encoding="utf-8")
    return root


def test_built_assets_are_served_with_spa_fallback(monkeypatch, tmp_path, auth_headers):
    from mailroom_diagnostics import app as app_module

    monkeypatch.setattr(app_module, "STATIC_ROOT", _built_tree(tmp_path))
    client = TestClient(app_module.create_app(), headers=auth_headers)

    index = client.get("/diagnostics")
    assert index.status_code == 200
    assert "diagnostics-ui" in index.text

    asset = client.get("/diagnostics/assets/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text

    deep_link = client.get("/diagnostics/run/execution-1")
    assert deep_link.status_code == 200
    assert "diagnostics-ui" in deep_link.text


def test_asset_paths_cannot_escape_the_static_root(monkeypatch, tmp_path, auth_headers):
    from mailroom_diagnostics import app as app_module

    monkeypatch.setattr(app_module, "STATIC_ROOT", _built_tree(tmp_path))
    outside = tmp_path / "secret.txt"
    outside.write_text("not-for-export", encoding="utf-8")
    client = TestClient(app_module.create_app(), headers=auth_headers)

    for path in ("/diagnostics/../secret.txt", "/diagnostics/%2e%2e/secret.txt"):
        response = client.get(path)
        assert response.status_code in (200, 404)
        assert "not-for-export" not in response.text
        if response.status_code == 200:
            assert "diagnostics-ui" in response.text


def test_static_package_directory_is_declared_and_present():
    """The wheel must carry static/; the app handles an absent index itself."""
    from importlib.resources import files

    package_static = files("mailroom_diagnostics").joinpath("static")
    assert package_static.joinpath("README.md").is_file()
    assert "Generated static assets" in package_static.joinpath("README.md").read_text(encoding="utf-8")
