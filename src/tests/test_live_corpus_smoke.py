"""Real-document routing/runtime smoke (network + live corpus, opt-in).

Runs the committed manifest fixture's real documents through the full graph in
mock-LLM mode, asserts the expected class/stage and the clean archived node
path, then replays a journal and requires the same path and outcome.

Enable with ``MAILROOM_LIVE_TESTS=1``. The test intentionally fetches the
pinned Hub parquet (v9.3) and verifies every content sha256; it does not use
synthetic document text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "src" / "tests" / "fixtures" / "live_manifest_sample.csv"
CLASSES = "contract,merger_agreement,correspondence,insurance_claim,corporate_record"

pytestmark = pytest.mark.skipif(
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
