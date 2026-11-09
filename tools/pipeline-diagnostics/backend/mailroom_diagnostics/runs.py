"""Read pipeline run journals and experiment summaries for the diagnostics API.

Journals are written by ``graph.run_events.RunEventRecorder``; experiments by
``scripts/smoke_live_documents.py``. Both are local, read-only artifacts. This
module never imports the graph or executes a pipeline.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

MAX_RUN_BYTES = 5 * 1024 * 1024
MAX_SCAN_FILES = 500
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _base() -> Path:
    base = os.environ.get("MAILROOM_BASE_DIR", "").strip()
    return Path(base).expanduser() if base else Path("data")


def runs_dir() -> Path:
    explicit = os.environ.get("MAILROOM_DIAGNOSTICS_RUNS_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else _base() / "diagnostics" / "runs"


def experiments_dir() -> Path:
    explicit = os.environ.get("MAILROOM_DIAGNOSTICS_EXPERIMENTS_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else _base() / "diagnostics" / "experiments"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_RUN_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _journal_paths(root: Path) -> list[Path]:
    """Newest-first, bounded scan: listing must not parse an unbounded archive."""
    try:
        paths = [path for path in root.glob("*.json") if path.is_file()]
    except OSError:
        return []
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:MAX_SCAN_FILES]


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_transitions(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("source"), str)
        and isinstance(item.get("target"), str)
        for item in value
    )


def _valid_run(payload: dict[str, Any]) -> bool:
    """A journal is listable/loadable only if the viewer can iterate every array it reads."""
    return (
        isinstance(payload.get("execution_id"), str)
        and isinstance(payload.get("events"), list)
        and _str_list(payload.get("node_sequence"))
        and _valid_transitions(payload.get("transitions"))
        and isinstance(payload.get("outcome"), dict)
        and isinstance(payload.get("document"), dict)
    )


def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    root = runs_dir()
    if not root.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in _journal_paths(root):
        if len(summaries) >= limit:
            break
        payload = _read_json(path)
        if payload is None or not _valid_run(payload):
            continue
        document = payload.get("document") or {}
        outcome = payload.get("outcome") or {}
        summaries.append(
            {
                "execution_id": payload["execution_id"],
                "document": {
                    "filename": document.get("filename"),
                    "matter_id": document.get("matter_id"),
                    "manifest_id": document.get("manifest_id"),
                    "source": document.get("source"),
                },
                "outcome": {
                    "stage": outcome.get("stage"),
                    "doc_type": outcome.get("doc_type"),
                    "review": bool(outcome.get("review")),
                    "failed": bool(outcome.get("failed")),
                },
                "started_at": payload.get("started_at"),
                "finished_at": payload.get("finished_at"),
                "node_count": len(payload.get("node_sequence") or []),
            }
        )
    summaries.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return summaries[: max(1, limit)]


def load_run(execution_id: str) -> dict[str, Any] | None:
    if not _ID.match(execution_id or ""):
        return None
    root = runs_dir()
    if not root.is_dir():
        return None
    for path in _journal_paths(root):
        payload = _read_json(path)
        if payload and payload.get("execution_id") == execution_id and _valid_run(payload):
            return payload
    return None


def _valid_experiment(payload: dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("experiment_id"), str)
        and isinstance(payload.get("runs"), list)
        and all(isinstance(run, dict) for run in payload["runs"])
        and isinstance(payload.get("metrics"), dict)
    )


def list_experiments(limit: int = 50) -> list[dict[str, Any]]:
    root = experiments_dir()
    if not root.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in _journal_paths(root):
        if len(summaries) >= limit:
            break
        payload = _read_json(path)
        if not payload or not _valid_experiment(payload):
            continue
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        corpus = payload.get("corpus") if isinstance(payload.get("corpus"), dict) else {}
        run_records = payload.get("runs") if isinstance(payload.get("runs"), list) else []
        summaries.append(
            {
                "experiment_id": payload["experiment_id"],
                "generated_at": payload.get("generated_at"),
                "mode": payload.get("mode"),
                "corpus": {
                    "name": corpus.get("name"),
                    "revision": corpus.get("revision"),
                },
                "metrics": metrics,
                "runs": len(run_records),
            }
        )
    summaries.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    return summaries[: max(1, limit)]


def load_experiment(experiment_id: str) -> dict[str, Any] | None:
    if not _ID.match(experiment_id or ""):
        return None
    root = experiments_dir()
    if not root.is_dir():
        return None
    for path in _journal_paths(root):
        payload = _read_json(path)
        if payload and payload.get("experiment_id") == experiment_id and _valid_experiment(payload):
            # Local paths are not part of the API contract.
            payload.pop("base_dir", None)
            corpus = payload.get("corpus")
            if isinstance(corpus, dict):
                source = corpus.get("source")
                if isinstance(source, str) and (
                    "/" in source or "\\" in source or (len(source) > 1 and source[1] == ":")
                ):
                    # Absolute or relative, the directory part is a local path.
                    corpus["source"] = source.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            for run in payload["runs"]:
                if isinstance(run, dict):
                    run.pop("journal", None)
            return payload
    return None
