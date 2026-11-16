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


def test_valid_journal_larger_than_limit_is_rejected_by_list_and_detail(dirs, client):
    from mailroom_diagnostics.runs import MAX_RUN_BYTES

    runs, _ = dirs
    journal = _journal("run-huge")
    journal["padding"] = "x" * (MAX_RUN_BYTES + 1)
    path = runs / "huge.json"
    path.write_text(json.dumps(journal), encoding="utf-8")
    assert path.stat().st_size > MAX_RUN_BYTES

    assert client.get("/v1/diagnostics/runs").json()["runs"] == []
    assert client.get("/v1/diagnostics/runs/run-huge").status_code == 404

    # The same journal under the limit is accepted, so the size is what rejected it.
    journal.pop("padding")
    path.write_text(json.dumps(journal), encoding="utf-8")
    assert client.get("/v1/diagnostics/runs").json()["runs"][0]["execution_id"] == "run-huge"
    assert client.get("/v1/diagnostics/runs/run-huge").status_code == 200


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


def test_object_journal_leaves_are_dropped_not_served(dirs, client):
    """A malformed leaf must not reach a renderer as an object/list.

    Regression for the live-manifest smoke journals whose ``document`` and
    ``outcome`` leaves were objects: the React panel rendered them as children
    and crashed. The API boundary now projects text leaves only.
    """
    runs, _ = dirs
    journal = _journal()
    journal["document"] = {
        "filename": {"nested": "x"},
        "matter_id": ["M"],
        "manifest_id": {"id": 1},
        "source": {"kind": "gmail"},
        "doc_id": {"id": "d"},
    }
    journal["outcome"] = {"stage": {"s": 1}, "doc_type": ["contract"], "review": False, "failed": False}
    journal["node_sequence"] = ["intake", "classify"]
    journal["events"] = [
        "not-an-event",
        {"type": "node_started", "node": {"bad": 1}},
        {"type": "node_completed", "node": "intake", "sequence": 1, "ts": 1.0},
    ]
    journal["transitions"] = [{"source": "intake", "target": "classify"}]
    journal["ground_truth"] = {"expected_doc_class": {"x": 1}, "expected_stage": "archived"}
    (runs / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    summary = client.get("/v1/diagnostics/runs").json()["runs"][0]
    assert summary["document"] == {
        "filename": None,
        "matter_id": None,
        "manifest_id": None,
        "doc_id": None,
        "source": None,
    }
    assert summary["outcome"]["stage"] is None
    assert summary["outcome"]["doc_type"] is None
    assert summary["node_count"] == 2

    detail = client.get("/v1/diagnostics/runs/run-abc123").json()
    assert detail["document"]["doc_id"] is None
    assert detail["outcome"]["stage"] is None
    assert detail["node_sequence"] == ["intake", "classify"]
    assert [event["node"] for event in detail["events"]] == ["intake"]
    assert detail["transitions"] == [{"source": "intake", "target": "classify"}]
    assert detail["ground_truth"] == {"expected_stage": "archived"}


def test_object_experiment_run_leaves_are_dropped(dirs, client):
    _, experiments = dirs
    payload = _experiment()
    payload["runs"] = [
        {
            "execution_id": {"bad": 1},
            "manifest_id": ["DOC-1"],
            "filename": {"x": 1},
            "expected": "contract",
            "predicted": {"class": "contract"},
            "stage": ["archived"],
            "node_sequence": ["intake", {"bad": 1}],
            "checks": {"class_ok": True, "stage_ok": "yes"},
        }
    ]
    (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")

    run = client.get("/v1/diagnostics/experiments/20261109T050000Z").json()["runs"][0]
    assert run["execution_id"] is None
    assert run["manifest_id"] is None
    assert run["predicted"] is None
    assert run["stage"] is None
    assert run["expected"] == "contract"
    assert run["node_sequence"] == ["intake"]
    assert run["checks"] == {"class_ok": True}


@pytest.mark.parametrize("bad_run", [None, "x", 3])
def test_experiment_with_a_malformed_run_is_rejected(dirs, client, bad_run):
    _, experiments = dirs
    payload = _experiment()
    payload["runs"].append(bad_run)
    (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")
    assert client.get("/v1/diagnostics/experiments").json()["experiments"] == []
    assert client.get("/v1/diagnostics/experiments/20261109T050000Z").status_code == 404


@pytest.mark.parametrize(
    "mutation",
    [
        {"node_sequence": 1},
        {"node_sequence": "intake"},
        {"node_sequence": ["intake", 2]},
        {"transitions": 1},
        {"transitions": "intake->classify"},
        {"transitions": [{"source": "intake"}]},
        {"transitions": ["intake"]},
    ],
)
def test_journal_with_malformed_arrays_is_skipped_not_fatal(dirs, client, mutation):
    runs, _ = dirs
    (runs / "good.json").write_text(json.dumps(_journal("run-good")), encoding="utf-8")
    (runs / "bad.json").write_text(
        json.dumps({**_journal("run-bad"), **mutation}), encoding="utf-8"
    )
    listing = client.get("/v1/diagnostics/runs")
    assert listing.status_code == 200
    assert [run["execution_id"] for run in listing.json()["runs"]] == ["run-good"]
    assert client.get("/v1/diagnostics/runs/run-bad").status_code == 404
    assert client.get("/v1/diagnostics/runs/run-good").status_code == 200


def test_journal_without_transitions_is_skipped(dirs, client):
    runs, _ = dirs
    bad = _journal()
    bad.pop("transitions")
    (runs / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    assert client.get("/v1/diagnostics/runs").json()["runs"] == []
    assert client.get("/v1/diagnostics/runs/run-abc123").status_code == 404


@pytest.mark.parametrize(
    "source",
    [
        "/private/client/manifest.csv",
        "src/tests/fixtures/manifest.csv",
        "../outside/manifest.csv",
        "C:\\Users\\someone\\manifest.csv",
        "data\\manifest.csv",
        "/private/client/manifests/",
    ],
)
def test_experiment_corpus_source_never_exposes_a_directory(dirs, client, source):
    _, experiments = dirs
    payload = _experiment()
    payload["corpus"]["source"] = source
    (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")
    served = client.get("/v1/diagnostics/experiments/20261109T050000Z").json()["corpus"]["source"]
    assert "/" not in served and "\\" not in served and ":" not in served
    assert "private" not in served and "outside" not in served and "Users" not in served


def test_experiment_corpus_source_label_is_kept(dirs, client):
    _, experiments = dirs
    for label in ("live", "manifest.csv"):
        payload = _experiment()
        payload["corpus"]["source"] = label
        (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")
        detail = client.get("/v1/diagnostics/experiments/20261109T050000Z").json()
        assert detail["corpus"]["source"] == label


def test_load_run_forwards_only_allowlisted_journal_fields(dirs, client):
    """A journal may carry extras; the HTTP response is a strict projection.

    Regression: ``load_run`` used to return the source dict after replacing a
    few keys, so ``raw_document`` and arbitrary ``event.output`` payloads were
    served to the browser.
    """
    runs, _ = dirs
    journal = _journal()
    journal["raw_document"] = "SECRET-DOCUMENT"
    journal["document"]["raw_path"] = "/private/SECRET-DOCUMENT"
    journal["outcome"]["raw_reason"] = "SECRET-DOCUMENT"
    journal["events"][0]["output"] = {
        "raw_text": "SECRET-DOCUMENT",
        "stage": "intake",
        "judge_verdict": "pass",
        "nested": {"secret": "SECRET-DOCUMENT"},
    }
    journal["ground_truth"]["raw_truth"] = "SECRET-DOCUMENT"
    (runs / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    listing = client.get("/v1/diagnostics/runs")
    assert "SECRET-DOCUMENT" not in listing.text

    detail = client.get("/v1/diagnostics/runs/run-abc123").json()
    assert "SECRET-DOCUMENT" not in json.dumps(detail)
    assert set(detail) == {
        "schema_version",
        "execution_id",
        "document",
        "outcome",
        "started_at",
        "finished_at",
        "events",
        "node_sequence",
        "transitions",
        "ground_truth",
    }
    assert detail["events"][0]["output"] == {"stage": "intake", "judge_verdict": "pass"}
    assert "raw_path" not in detail["document"]
    assert "raw_reason" not in detail["outcome"]
    assert detail["ground_truth"] == {
        "expected_doc_class": "contract",
        "expected_stage": "archived",
    }


def test_load_experiment_forwards_only_allowlisted_fields(dirs, client):
    _, experiments = dirs
    payload = _experiment()
    payload["raw_document"] = "SECRET-DOCUMENT"
    payload["corpus"]["secret_path"] = "/private/SECRET-DOCUMENT"
    payload["corpus"]["revision"] = {"tag": "v9.3", "blob": {"secret": "SECRET-DOCUMENT"}}
    payload["metrics"]["nested_blob"] = {"secret": "SECRET-DOCUMENT"}
    payload["runs"][0]["raw_text"] = "SECRET-DOCUMENT"
    (experiments / "experiment.json").write_text(json.dumps(payload), encoding="utf-8")

    listing = client.get("/v1/diagnostics/experiments")
    assert "SECRET-DOCUMENT" not in listing.text

    detail = client.get("/v1/diagnostics/experiments/20261109T050000Z").json()
    assert "SECRET-DOCUMENT" not in json.dumps(detail)
    assert set(detail) == {
        "schema_version",
        "experiment_id",
        "generated_at",
        "mode",
        "corpus",
        "metrics",
        "runs",
    }
    assert detail["corpus"]["revision"] == {"tag": "v9.3"}
    assert "nested_blob" not in detail["metrics"]
    assert "raw_text" not in detail["runs"][0]
    assert detail["metrics"]["all_checks_ok"] is True
