"""Run/experiment journals are read-only, bounded and path-safe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def auth_headers(monkeypatch):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "runs-token")
    return {"Authorization": "Bearer runs-token"}


def _journal(execution_id: str = "run-abc123") -> dict:
    return {
        "schema_version": "1.0.0",
        "execution_id": execution_id,
        "document": {
            "filename": "real-doc.txt",
            "matter_id": "LIVE-1",
            "manifest_id": "DOC-1",
            "source": "live-manifest",
        },
        "ground_truth": {"expected_doc_class": "contract", "expected_stage": "archived"},
        "started_at": "2026-11-09T05:00:00+00:00",
        "finished_at": "2026-11-09T05:00:01+00:00",
        "events": [
            {"type": "node_started", "sequence": 1, "node": "intake", "ts": 1.0},
            {"type": "node_completed", "sequence": 1, "node": "intake", "ts": 1.1},
        ],
        "node_sequence": ["intake", "classify"],
        "transitions": [{"source": "intake", "target": "classify"}],
        "outcome": {"stage": "archived", "doc_type": "contract", "review": False, "failed": False},
    }


def _experiment(experiment_id: str = "20261109T050000Z") -> dict:
    return {
        "schema_version": "1.0.0",
        "experiment_id": experiment_id,
        "generated_at": experiment_id,
        "mode": "mock",
        "corpus": {"name": "Lucius-Morningstar/mailroom-dataset", "revision": {"tag": "v9.3"}},
        "base_dir": "/private/secret",
        "runs": [{"manifest_id": "DOC-1", "journal": "/private/secret/run.json"}],
        "metrics": {"n": 1, "class_ok": 1, "stage_ok": 1, "all_checks_ok": True},
    }


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    experiments = tmp_path / "experiments"
    runs.mkdir()
    experiments.mkdir()
    monkeypatch.setenv("MAILROOM_DIAGNOSTICS_RUNS_DIR", str(runs))
    monkeypatch.setenv("MAILROOM_DIAGNOSTICS_EXPERIMENTS_DIR", str(experiments))
    return runs, experiments


@pytest.fixture
def client(auth_headers):
    from mailroom_diagnostics.app import create_app

    return TestClient(create_app(), headers=auth_headers)


def test_runs_list_and_load(dirs, client):
    runs, _ = dirs
    (runs / "journal.json").write_text(json.dumps(_journal()), encoding="utf-8")

    listing = client.get("/v1/diagnostics/runs")
    assert listing.status_code == 200
    assert listing.json()["runs"][0]["execution_id"] == "run-abc123"
    assert listing.json()["runs"][0]["outcome"]["stage"] == "archived"

    detail = client.get("/v1/diagnostics/runs/run-abc123")
    assert detail.status_code == 200
    assert detail.json()["node_sequence"] == ["intake", "classify"]
    assert client.get("/v1/diagnostics/runs/missing").status_code == 404
    assert client.get("/v1/diagnostics/runs/..%2Fsecret").status_code == 404


def test_corrupt_and_oversized_journals_are_ignored(dirs, client):
    runs, _ = dirs
    (runs / "broken.json").write_text("{not json", encoding="utf-8")
    (runs / "missing.json").write_text(json.dumps({"execution_id": "x"}), encoding="utf-8")
    assert client.get("/v1/diagnostics/runs").json()["runs"] == []


def test_oversized_journal_is_ignored(dirs, client):
    from mailroom_diagnostics.runs import MAX_RUN_BYTES

    runs, _ = dirs
    (runs / "huge.json").write_bytes(b"x" * (MAX_RUN_BYTES + 1))
    assert client.get("/v1/diagnostics/runs").json()["runs"] == []


def test_incomplete_experiment_is_not_listed_or_loaded(dirs, client):
    _, experiments = dirs
    (experiments / "partial.json").write_text(
        json.dumps({"experiment_id": "partial"}), encoding="utf-8"
    )
    assert client.get("/v1/diagnostics/experiments").json()["experiments"] == []
    assert client.get("/v1/diagnostics/experiments/partial").status_code == 404


def test_experiment_load_strips_local_paths(dirs, client):
    _, experiments = dirs
    payload = _experiment()
    payload["corpus"]["source"] = "/private/client/manifest.csv"
    (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")

    listing = client.get("/v1/diagnostics/experiments")
    assert listing.status_code == 200
    assert listing.json()["experiments"][0]["experiment_id"] == "20261109T050000Z"

    detail = client.get("/v1/diagnostics/experiments/20261109T050000Z")
    payload = detail.json()
    assert "base_dir" not in payload
    assert "journal" not in payload["runs"][0]
    assert payload["metrics"]["all_checks_ok"] is True
    assert payload["corpus"]["source"] == "manifest.csv"


def test_journal_without_node_sequence_is_ignored(dirs, client):
    runs, _ = dirs
    bad = _journal()
    bad.pop("node_sequence")
    (runs / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    assert client.get("/v1/diagnostics/runs").json()["runs"] == []
    assert client.get("/v1/diagnostics/runs/run-abc123").status_code == 404


def test_experiment_without_runs_is_rejected(dirs, client):
    _, experiments = dirs
    payload = _experiment()
    payload.pop("runs")
    (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")
    assert client.get("/v1/diagnostics/experiments").json()["experiments"] == []
    assert client.get("/v1/diagnostics/experiments/20261109T050000Z").status_code == 404
