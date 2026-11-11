"""Roadmap M9: trace sinks (spec §5.5)."""

from __future__ import annotations

import hashlib
import json
import socket
import threading

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, temp_base_dir):
    for name in ("MAILROOM_TRACE_SINKS", "MAILROOM_AUDIT_FULL_TRACE", "SPACE_ID", "HF_SPACE_ID",
                 "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PHOENIX_ENDPOINT", "http://127.0.0.1:9/v1/traces")  # nothing listens on port 9
    from audit.sinks import phoenix

    phoenix._probe_cache.clear()
    monkeypatch.setattr("audit.sinks._entry_points", lambda: [])


@pytest.fixture
def collector(monkeypatch):
    """A fake Phoenix collector on localhost: accepts TCP connections, nothing else."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    stop = threading.Event()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    monkeypatch.setenv("PHOENIX_ENDPOINT", f"http://127.0.0.1:{server.getsockname()[1]}/v1/traces")
    monkeypatch.setenv("PHOENIX_TRACING", "enabled")
    yield
    stop.set()
    thread.join(timeout=1)
    server.close()


def _names():
    from audit.sinks import active_sinks

    return [s.name for s in active_sinks()]


def test_sql_sink_always_active(monkeypatch):
    from audit.sinks import record_io
    from audit.sinks.sql import SqlSink

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    assert _names() == ["sql"]
    sink, sha, also = record_io(visit_id="v-1", doc_id="d-1", prompt="p", completion="c")
    assert (sink, also) == ("sql", [])
    stored = SqlSink().fetch_io("v-1")
    assert (stored["prompt"], stored["completion"], stored["sha256"], stored["doc_id"]) == ("p", "c", sha, "d-1")


def test_phoenix_added_when_importable_enabled_and_reachable(collector, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    assert _names() == ["sql", "phoenix"]
    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    assert _names() == ["sql"]


def test_phoenix_skipped_when_unreachable(monkeypatch):
    from audit.sinks.phoenix import PROBE_TIMEOUT_S

    monkeypatch.setenv("PHOENIX_TRACING", "enabled")
    assert _names() == ["sql"]
    assert PROBE_TIMEOUT_S == 1.0


def test_phoenix_skipped_on_railway_localhost_endpoint(collector, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert _names() == ["sql"]  # a localhost collector cannot exist in a hosted container
    monkeypatch.delenv("RAILWAY_ENVIRONMENT")
    monkeypatch.setenv("SPACE_ID", "org/space")
    assert _names() == ["sql"]


def test_env_enables_opt_in_sinks(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", "sql,langfuse")
    assert _names() == ["sql", "langfuse"]
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", "otlp, braintrust ,sql")
    assert _names() == ["sql", "braintrust", "otlp"]


class _MemorySink:
    name = "memory"

    def __init__(self):
        self.io = {}

    def record_io(self, visit_id, doc_id, prompt, completion):
        self.io[visit_id] = (prompt, completion)
        return visit_id

    def capture_pending(self, record):
        return None

    def fetch_pending(self):
        return []

    def fetch_io(self, ref):
        return None


class _EntryPoint:
    def __init__(self, name, obj):
        self.name, self._obj = name, obj

    def load(self):
        return self._obj


def test_entry_point_sink_loaded(monkeypatch):
    from audit.sinks import record_io

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    monkeypatch.setattr("audit.sinks._entry_points", lambda: [
        _EntryPoint("memory", _MemorySink), _EntryPoint("sql", object),  # a built-in name is not loaded twice
    ])
    assert _names() == ["sql", "memory"]
    sink, _, also = record_io(visit_id="v-ep", doc_id="d", prompt="p", completion="c")
    assert (sink, also) == ("sql", ["memory"])


def test_sink_failure_does_not_fail_node(monkeypatch):
    from audit.sinks import record_io
    from audit.sinks.sql import SqlSink
    from observability.tracing import traced_node
    from pipeline.limits import record_usage, reset_run_usage

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")

    def boom(self, *a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(SqlSink, "record_io", boom)
    sink, sha, also = record_io(visit_id="v-x", doc_id="d", prompt="p", completion="c")
    assert (sink, also) == (None, []) and sha

    class Usage:
        prompt_tokens, completion_tokens = 1, 1

    reset_run_usage()

    @traced_node("classify-document")
    def classify_node(state):
        record_usage(Usage(), "m", prompt="p", completion="c")
        return {"doc_type": "contract"}

    (v,) = classify_node({"doc_id": "d", "audit_trace": [{"node": "intake"}]})["audit_trace"]
    assert v["trace_sink"] is None and v["io_sha256"] == sha


def test_full_trace_copies_io_into_visit(monkeypatch):
    from audit.trace import visit
    from datetime import datetime, timezone

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    now = datetime.now(timezone.utc)
    llm = {"prompt": "the prompt", "completion": "the completion", "model": "m"}
    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "1")
    v = visit("classify", {"doc_id": "d"}, {}, started_at=now, ended_at=now, llm=llm)
    assert (v["prompt"], v["completion"]) == ("the prompt", "the completion") and v["trace_sink"] == "sql"
    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "0")
    v = visit("classify", {"doc_id": "d"}, {}, started_at=now, ended_at=now, llm=llm)
    assert "prompt" not in v and "completion" not in v and v["trace_sink"] == "sql"


def test_full_trace_default_from_taxonomy():
    from audit.trace import full_trace_enabled
    from pipeline.config import load_config

    assert (load_config().get("audit") or {}).get("full_trace") is False
    assert full_trace_enabled() is False


def test_io_sha_is_sha256_of_prompt_and_completion(monkeypatch):
    from audit.digests import io_sha256
    from audit.sinks import record_io

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    _, sha, _ = record_io(visit_id="v-sha", doc_id="d", prompt="ab", completion="cd")
    assert sha == io_sha256("ab", "cd")
    # SHA-256 of the canonical JSON of both halves, so the split point counts.
    assert sha == hashlib.sha256(json.dumps({"completion": "cd", "prompt": "ab"}).encode()).hexdigest()
    assert io_sha256("a", "bcd") != sha


def test_pending_rows_round_trip_through_spool(monkeypatch, tmp_path):
    from audit.sinks import capture_pending
    from audit.sinks.sql import SqlSink, spool_path

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "spool.db"))
    assert spool_path() == tmp_path / "spool.db"
    (kept,) = capture_pending({"doc_id": "d-p", "event": "archived"})
    assert (kept["sink"], kept["complete"]) == ("sql", True) and kept["ref"].startswith("pend-")
    (pending,) = SqlSink().fetch_pending()
    assert pending["doc_id"] == "d-p" and pending["ref"].startswith("pend-")
    SqlSink().drop_pending(pending["ref"])
    assert SqlSink().fetch_pending() == []
