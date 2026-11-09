"""Real-document routing/runtime smoke (network + live corpus, opt-in).

Runs the committed manifest fixture's real documents through the full graph in
mock-LLM mode, asserts the expected class/stage and the clean archived node
path, then replays a journal and requires the same path and outcome.

Enable with ``MAILROOM_LIVE_TESTS=1``. The test intentionally fetches the
pinned Hub parquet (v9.3) and verifies every content sha256; it does not use
synthetic document text.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "src" / "tests" / "fixtures" / "live_manifest_sample.csv"
CLASSES = "contract,merger_agreement,correspondence,insurance_claim,corporate_record"

LIVE = pytest.mark.skipif(
    os.environ.get("MAILROOM_LIVE_TESTS") != "1",
    reason="live corpus test: set MAILROOM_LIVE_TESTS=1 (fetches pinned real documents)",
)

CLEAN_ARCHIVE_PATH = [
    "intake",
    "classify",
    "extract",
    "compile_report",
    "catalog_write",
    "archive",
]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "OBSERVABILITY_PROVIDER": "none",
            "MAILROOM_RELATIONS": "0",
            "MAILROOM_RELATIONS_EMBEDDINGS": "0",
            "MAILROOM_GMAIL_ENABLED": "0",
            "LOG_LEVEL": "CRITICAL",
        }
    )
    return env


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "src/scripts/smoke_live_documents.py", *args],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@LIVE
@pytest.mark.live_corpus
def test_real_documents_route_to_expected_outcomes_and_replay(tmp_path):
    base = tmp_path / "base"
    out = tmp_path / "out"
    proc = _run(
        [
            "--manifest",
            str(FIXTURE),
            "--per-class",
            "1",
            "--classes",
            CLASSES,
            "--max-chars",
            "250000",
            "--base",
            str(base),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 0, proc.stderr[-4000:]

    experiment = json.loads((out / "experiment.json").read_text(encoding="utf-8"))
    assert experiment["metrics"] == {
        "n": 5,
        "class_ok": 5,
        "stage_ok": 5,
        "all_checks_ok": True,
        "classes": [
            "contract",
            "corporate_record",
            "correspondence",
            "insurance_claim",
            "merger_agreement",
        ],
    }
    assert all(run["ok"] for run in experiment["runs"])
    for run in experiment["runs"]:
        assert run["predicted"] == run["expected"], run
        assert run["stage"] == "archived", run
        assert run["node_sequence"] == CLEAN_ARCHIVE_PATH, run
        assert run["outcome"]["doc_type"] == run["expected"]
        assert run["checks"]["terminal_node_ok"] is True

    journals = sorted((base / "diagnostics" / "runs").glob("*.json"))
    assert len(journals) == 5
    for path in journals:
        journal = json.loads(path.read_text(encoding="utf-8"))
        assert journal["events"]
        assert journal["node_sequence"] == CLEAN_ARCHIVE_PATH
        assert journal["outcome"]["stage"] == "archived"
        assert journal["document"]["content_sha256"]

    replay_out = tmp_path / "replay-out"
    replay = _run(
        [
            "--replay",
            str(journals[0]),
            "--manifest",
            str(FIXTURE),
            "--base",
            str(tmp_path / "replay-base"),
            "--out",
            str(replay_out),
        ]
    )
    assert replay.returncode == 0, replay.stderr[-4000:]
    report = json.loads((replay_out / "replay.json").read_text(encoding="utf-8"))
    assert report["same_sequence"] is True
    assert report["same_stage"] is True
    assert report["same_class"] is True
    assert report["new_sequence"] == CLEAN_ARCHIVE_PATH


def test_unknown_class_selection_is_rejected_without_running(tmp_path):
    proc = _run(
        [
            "--manifest",
            str(FIXTURE),
            "--classes",
            "not-a-class",
            "--per-class",
            "1",
            "--base",
            str(tmp_path / "base"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert proc.returncode == 2
    assert "unknown classes" in proc.stdout.lower()


def _replay_module():
    import importlib

    return importlib.import_module("scripts.smoke_live_documents")


def _replay_args(tmp_path, journal_document: dict, row_sha: str | None):
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps({"document": journal_document, "node_sequence": [], "outcome": {}}),
        encoding="utf-8",
    )
    row = {"id": "DOC-1", "filename": "a.txt", "content_sha256": row_sha}
    return journal, [row]


@pytest.mark.parametrize(
    ("journal_sha", "row_sha", "message"),
    [
        (None, "a" * 64, "records no content sha256"),
        ("a" * 64, "", "no content sha256 to verify"),
        ("a" * 64, "b" * 64, "does not match"),
    ],
)
def test_replay_rejects_missing_or_mismatched_content_hash(
    tmp_path, monkeypatch, journal_sha, row_sha, message
):
    smoke = _replay_module()
    document = {"manifest_id": "DOC-1"}
    if journal_sha:
        document["content_sha256"] = journal_sha
    journal, rows = _replay_args(tmp_path, document, row_sha)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("replay must be refused before any document is fetched or run")

    monkeypatch.setattr(smoke, "fetch_texts", _must_not_run)
    monkeypatch.setattr(smoke, "_run_one", _must_not_run)
    with pytest.raises(SystemExit) as excinfo:
        smoke._replay(
            journal, rows, {"tag": "v9.3"}, tmp_path / "base", tmp_path / "out",
            argparse.Namespace(real=False),
        )
    assert message in str(excinfo.value)


def test_replay_accepts_matching_content_hash(tmp_path, monkeypatch):
    smoke = _replay_module()
    sha = "c" * 64
    journal, rows = _replay_args(tmp_path, {"manifest_id": "DOC-1", "content_sha256": sha}, sha)
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(smoke, "fetch_texts", lambda r, rev: {"a.txt": "text"})
    monkeypatch.setattr(
        smoke,
        "_run_one",
        lambda *a, **k: ({"stage": None, "doc_type": None}, {"node_sequence": [], "outcome": {}}),
    )
    code = smoke._replay(
        journal, rows, {"tag": "v9.3"}, tmp_path / "base", tmp_path / "out",
        argparse.Namespace(real=False),
    )
    assert code == 0
