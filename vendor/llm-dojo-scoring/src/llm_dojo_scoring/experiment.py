"""Experiment-log record helpers: JSONL append/load, dotted access, snapshots.

Ported from ``src/experiment_log.py`` (llm-entity-extraction) — the parts the
scoring suite owns. The record SCHEMA is the contract preserved here; the
Excel-facing summary is defined in :mod:`llm_dojo_scoring.export`.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JSONL_ENV = "EXPERIMENT_LOG_PATH"
DEFAULT_JSONL = "reports/experiment_log.jsonl"


def default_jsonl_path() -> Path:
    """Resolve the JSONL log path from env (or the repo default)."""
    return Path(os.environ.get(JSONL_ENV, DEFAULT_JSONL))


def git_snapshot() -> dict:
    """Best-effort repo state at run time (commit hash + dirty flag)."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        )
        return {"commit": commit or None, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return {"commit": None, "dirty": None}


def mean(values: list[float]) -> float:
    """Arithmetic mean over a list of numbers (0.0 for an empty list)."""
    return sum(values) / len(values) if values else 0.0


def utc_now() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def append_experiment(record: dict, path: str | Path | None = None) -> Path:
    """Append one JSON record to the experiment log (one line per run).

    The record is stamped with an ISO timestamp if absent. Returns the path
    actually written.
    """
    path = Path(path or default_jsonl_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record.setdefault("timestamp", utc_now())
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return path


def load_records(path: str | Path) -> list[dict]:
    """Load experiment-log records (chronological order preserved)."""
    records: list[dict] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def dotted_get(record: dict, path: str, default: Any = None) -> Any:
    """Dotted-path getter into a record (``scores.sorter.subtype_accuracy``)."""
    cur: Any = record
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def record_date(record: dict):
    """The record's timestamp as a naive UTC date (or None)."""
    ts = record.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
    except (ValueError, TypeError):
        return None


__all__ = [
    "default_jsonl_path", "git_snapshot", "mean", "utc_now",
    "append_experiment", "load_records", "dotted_get", "record_date",
]
