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

# Response allowlists. A journal on disk may carry anything; the API returns
# only what the recorder and the screen define. ``RunEventRecorder`` curates
# ``output`` to these keys (see graph/run_events.py); they are repeated here
# so this reader never imports the graph just for a constant.
_EVENT_OUTPUT_KEYS = (
    "stage",
    "doc_type",
    "classification_confidence",
    "extraction_confidence",
    "judge_verdict",
    "arbiter_decision",
    "review_decision",
    "run_aborted",
)
_GROUND_TRUTH_KEYS = ("expected_doc_class", "expected_stage", "expected_subclass")
_EXPERIMENT_RUN_KEYS = (
    "execution_id",
    "manifest_id",
    "filename",
    "expected",
    "predicted",
    "stage",
    "node_sequence",
    "checks",
    "ok",
)
MAX_METRIC_FIELDS = 50
MAX_FIELD_NAME = 64


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


def _leaf_str(value: Any) -> str | None:
    """A journal leaf the screen can render as text, or None.

    Journals are written by pipeline code, but a reader must not trust their
    shape: an unexpected object/list leaf would be rendered as a React child
    and crash the panel. Non-strings are dropped rather than stringified.
    """
    return value if isinstance(value, str) else None


def _leaf_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _document_leaves(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": _leaf_str(document.get("filename")),
        "matter_id": _leaf_str(document.get("matter_id")),
        "manifest_id": _leaf_str(document.get("manifest_id")),
        "doc_id": _leaf_str(document.get("doc_id")),
        "source": _leaf_str(document.get("source")),
        "content_sha256": _leaf_str(document.get("content_sha256")),
        "hub_revision": _leaf_str(document.get("hub_revision")),
    }


def _outcome_leaves(outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": _leaf_str(outcome.get("stage")),
        "doc_type": _leaf_str(outcome.get("doc_type")),
        "review": bool(outcome.get("review")),
        "failed": bool(outcome.get("failed")),
        "run_aborted": outcome.get("run_aborted")
        if isinstance(outcome.get("run_aborted"), bool)
        else None,
        "node_count": _leaf_number(outcome.get("node_count")),
    }


def _curated_events(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    out: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        node = _leaf_str(event.get("node"))
        if node is None:
            continue
        curated: dict[str, Any] = {"node": node}
        kind = _leaf_str(event.get("type"))
        if kind is not None:
            curated["type"] = kind
        sequence = _leaf_number(event.get("sequence"))
        if sequence is not None:
            curated["sequence"] = sequence
        timestamp = _leaf_number(event.get("ts"))
        if timestamp is not None:
            curated["ts"] = timestamp
        duration = _leaf_number(event.get("duration_ms"))
        if duration is not None:
            curated["duration_ms"] = duration
        error_class = _leaf_str(event.get("error_class"))
        if error_class is not None:
            curated["error_class"] = error_class
        output = _curated_event_output(event.get("output"))
        if output:
            curated["output"] = output
        out.append(curated)
    return out


def _curated_event_output(value: Any) -> dict[str, Any]:
    """Primitive values for the recorder's allowlisted output keys only."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _EVENT_OUTPUT_KEYS:
        item = value.get(key)
        if isinstance(item, bool) or isinstance(item, (int, float, str)):
            out[key] = item
    return out


def _node_sequence(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _transitions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source, target = _leaf_str(item.get("source")), _leaf_str(item.get("target"))
        if source is not None and target is not None:
            out.append({"source": source, "target": target})
    return out


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
        document = _document_leaves(payload.get("document") or {})
        outcome = _outcome_leaves(payload.get("outcome") or {})
        summaries.append(
            {
                "execution_id": payload["execution_id"],
                "document": {
                    "filename": document["filename"],
                    "matter_id": document["matter_id"],
                    "manifest_id": document["manifest_id"],
                    "doc_id": document["doc_id"],
                    "source": document["source"],
                },
                "outcome": {
                    "stage": outcome["stage"],
                    "doc_type": outcome["doc_type"],
                    "review": outcome["review"],
                    "failed": outcome["failed"],
                },
                "started_at": _leaf_str(payload.get("started_at")),
                "finished_at": _leaf_str(payload.get("finished_at")),
                "node_count": len(_node_sequence(payload.get("node_sequence"))),
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
        if not payload or payload.get("execution_id") != execution_id or not _valid_run(payload):
            continue
        return _project_run(payload)
    return None


def _project_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the journal from the documented response allowlist.

    The on-disk journal is a pipeline artifact; the HTTP response is a contract.
    Returning the source object (even after replacing a few keys) forwards any
    extra keys — ``raw_document``, uncurated event output and so on. Only the
    fields the recorder defines and the screen renders leave this module.
    """
    ground_truth = payload.get("ground_truth")
    return {
        "schema_version": _leaf_str(payload.get("schema_version")) or "1.0.0",
        "execution_id": payload["execution_id"],
        "document": _document_leaves(payload.get("document") or {}),
        "outcome": _outcome_leaves(payload.get("outcome") or {}),
        "started_at": _leaf_str(payload.get("started_at")),
        "finished_at": _leaf_str(payload.get("finished_at")),
        "events": _curated_events(payload.get("events")),
        "node_sequence": _node_sequence(payload.get("node_sequence")),
        "transitions": _transitions(payload.get("transitions")),
        "ground_truth": {
            key: _leaf_str(ground_truth.get(key))
            for key in _GROUND_TRUTH_KEYS
            if isinstance(ground_truth, dict) and _leaf_str(ground_truth.get(key)) is not None
        }
        if isinstance(ground_truth, dict)
        else {},
    }


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
                "generated_at": _leaf_str(payload.get("generated_at")),
                "mode": _leaf_str(payload.get("mode")),
                "corpus": {
                    "name": _leaf_str(corpus.get("name")),
                    "revision": _scalar_metadata(corpus.get("revision")),
                },
                "metrics": _scalar_metrics(metrics),
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
            return _project_experiment(payload)
    return None


def _scalar_metadata(value: Any) -> Any:
    """A bounded scalar, or a shallow object of scalars (``{"tag": ...}``)."""
    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            key: item
            for key, item in list(value.items())[:10]
            if isinstance(key, str)
            and len(key) <= MAX_FIELD_NAME
            and (isinstance(item, bool) or isinstance(item, (int, float, str)))
        }
    return None


def _scalar_metrics(metrics: Any) -> dict[str, Any]:
    """Scalar metrics only: a listing must not forward nested experiment blobs."""
    if not isinstance(metrics, dict):
        return {}
    return {
        key: value
        for key, value in list(metrics.items())[:MAX_METRIC_FIELDS]
        if isinstance(key, str)
        and len(key) <= MAX_FIELD_NAME
        and (isinstance(value, bool) or isinstance(value, (int, float, str)))
    }


def _corpus_source(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    if "/" in value or "\\" in value or (len(value) > 1 and value[1] == ":"):
        return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return value


def _project_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the experiment detail from the documented response allowlist."""
    corpus = payload.get("corpus") if isinstance(payload.get("corpus"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    return {
        "schema_version": _leaf_str(payload.get("schema_version")) or "1.0.0",
        "experiment_id": payload["experiment_id"],
        "generated_at": _leaf_str(payload.get("generated_at")),
        "mode": _leaf_str(payload.get("mode")),
        "corpus": {
            "name": _leaf_str(corpus.get("name")),
            "revision": _scalar_metadata(corpus.get("revision")),
            "source": _corpus_source(corpus.get("source")),
        },
        "metrics": _scalar_metrics(metrics),
        "runs": [
            _project_experiment_run(run)
            for run in runs
            if isinstance(run, dict)
        ],
    }


def _project_experiment_run(run: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {key: _leaf_str(run.get(key)) for key in _EXPERIMENT_RUN_KEYS}
    projected["node_sequence"] = _node_sequence(run.get("node_sequence"))
    checks = run.get("checks")
    projected["checks"] = (
        {
            key: value
            for key, value in checks.items()
            if isinstance(key, str) and isinstance(value, bool)
        }
        if isinstance(checks, dict)
        else {}
    )
    projected["ok"] = run["ok"] if isinstance(run.get("ok"), bool) else bool(run.get("ok"))
    return projected
