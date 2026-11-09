"""Opt-in pipeline run journals for exact node ingress and outcome tracking.

The recorder is a LangChain callback handler attached to one graph run. It
records the exact LangGraph node ids (``metadata["langgraph_node"]``), their
order, durations and curated stage/confidence summaries — never document text,
extracted values, gold payloads or absolute paths.

Journals land as one JSON file per run under ``<base>/diagnostics/runs`` (or
``MAILROOM_DIAGNOSTICS_RUNS_DIR``). The diagnostics API and React viewer read
them; replay re-runs the recorded document and compares the node sequence and
outcome.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # langchain-core is a pipeline dependency; keep journal reads import-safe.
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - diagnostics readers without the agent stack
    BaseCallbackHandler = object  # type: ignore[assignment,misc]

RUN_EVENTS_SCHEMA_VERSION = "1.0.0"
_CURATED_OUTPUT_KEYS = (
    "stage",
    "doc_type",
    "classification_confidence",
    "extraction_confidence",
    "judge_verdict",
    "arbiter_decision",
    "review_decision",
    "run_aborted",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_runs_dir() -> Path:
    explicit = os.environ.get("MAILROOM_DIAGNOSTICS_RUNS_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("MAILROOM_BASE_DIR", "").strip()
    return (Path(base).expanduser() if base else Path("data")) / "diagnostics" / "runs"


def _curated(outputs: Any) -> dict[str, Any]:
    if not isinstance(outputs, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _CURATED_OUTPUT_KEYS:
        value = outputs.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float, str)):
            out[key] = value
    return out


class RunEventRecorder(BaseCallbackHandler):  # type: ignore[misc]
    """Callback handler + journal writer for one pipeline run.

    Deliberately duck-typed rather than importing LangChain at module scope so
    the diagnostics CLI/API can read journals without the agent stack.
    """

    def __init__(
        self,
        document: dict[str, Any] | None = None,
        *,
        ground_truth: dict[str, Any] | None = None,
        runs_dir: Path | None = None,
        execution_id: str | None = None,
    ) -> None:
        self.document = {
            key: value
            for key, value in (document or {}).items()
            if key
            in {
                "filename",
                "matter_id",
                "doc_id",
                "source",
                "manifest_id",
                "content_sha256",
                "hub_revision",
            }
            and value not in (None, "")
        }
        self.ground_truth = {
            key: value
            for key, value in (ground_truth or {}).items()
            if key in {"expected_doc_class", "expected_stage", "expected_subclass"}
            and value not in (None, "")
        }
        self.runs_dir = Path(runs_dir) if runs_dir is not None else default_runs_dir()
        self.execution_id = execution_id or uuid.uuid4().hex
        self.started_at = _now_iso()
        self.finished_at: str | None = None
        self.events: list[dict[str, Any]] = []
        self._starts: dict[str, tuple[float, int, str]] = {}
        self._sequence = 0
        self._lock = threading.Lock()
        self.payload: dict[str, Any] | None = None

    # -- LangChain callback surface -------------------------------------
    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = (metadata or {}).get("langgraph_node")
        if not node or str(node) in {"__start__", "__end__"}:
            return
        # LangGraph emits a nested sequencing chain for each node step; only
        # the node run carries a ``graph:step`` tag.
        if not any(str(tag).startswith("graph:step:") for tag in tags or []):
            return
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._starts[str(run_id)] = (time.perf_counter(), sequence, str(node))
            self.events.append(
                {
                    "type": "node_started",
                    "sequence": sequence,
                    "node": str(node),
                    "ts": time.time(),
                }
            )

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        started = self._starts.pop(str(run_id), None)
        if started is None:
            return
        started_perf, sequence, node = started
        with self._lock:
            self.events.append(
                {
                    "type": "node_completed",
                    "sequence": sequence,
                    "node": node,
                    "duration_ms": round((time.perf_counter() - started_perf) * 1000, 3),
                    "output": _curated(outputs),
                    "ts": time.time(),
                }
            )

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        started = self._starts.pop(str(run_id), None)
        if started is None:
            return
        _started_perf, sequence, node = started
        with self._lock:
            self.events.append(
                {
                    "type": "node_errored",
                    "sequence": sequence,
                    "node": node,
                    "error_class": type(error).__name__,
                    "ts": time.time(),
                }
            )

    # -- journal -------------------------------------------------------
    def finalize(self, result: dict[str, Any] | None = None) -> dict[str, Any]:
        result = result or {}
        self.finished_at = _now_iso()
        events = sorted(self.events, key=lambda event: (event["sequence"], event["ts"]))
        node_sequence = [event["node"] for event in events if event["type"] == "node_started"]
        transitions = [
            {"source": source, "target": target}
            for source, target in zip(node_sequence, node_sequence[1:])
        ]
        stage = result.get("stage")
        outcome = {
            "stage": stage,
            "doc_type": result.get("doc_type"),
            "run_aborted": bool(result.get("run_aborted")),
            "review": stage == "review",
            "failed": stage == "failed",
            "node_count": len(node_sequence),
        }
        document = dict(self.document)
        if result.get("doc_id") and not document.get("doc_id"):
            document["doc_id"] = result.get("doc_id")
        payload = {
            "schema_version": RUN_EVENTS_SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "document": document,
            "ground_truth": self.ground_truth,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "events": events,
            "node_sequence": node_sequence,
            "transitions": transitions,
            "outcome": outcome,
        }
        self.payload = payload
        self._write(payload)
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            label = str(payload.get("document", {}).get("doc_id") or "run")
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in label)[:60]
            target = self.runs_dir / f"{safe}-{self.execution_id[:12]}.json"
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            # Diagnostics must never fail the run.
            pass
