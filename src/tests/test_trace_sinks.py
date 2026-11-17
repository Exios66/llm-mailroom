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


@pytest.fixture
def full_trace(monkeypatch):
    """Sinks only receive prompt/completion text with ``audit.full_trace`` on (#239 review)."""
    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "1")


def _names():
    from audit.sinks import active_sinks

    return [s.name for s in active_sinks()]


def test_sql_sink_always_active(monkeypatch, full_trace):
    from audit.sinks import record_io
    from audit.sinks.sql import SqlSink

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    assert _names() == ["sql"]
    sink, sha, also = record_io(visit_id="v-1", doc_id="d-1", prompt="p", completion="c")
    assert (sink, also) == ("sql", [])
    stored = SqlSink().fetch_io("v-1")
    assert (stored["prompt"], stored["completion"], stored["sha256"], stored["doc_id"]) == ("p", "c", sha, "d-1")


def test_phoenix_added_when_named_importable_enabled_and_reachable(collector, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", "sql,phoenix")
    assert _names() == ["sql", "phoenix"]
    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    assert _names() == ["sql"]


def test_phoenix_is_not_added_without_an_explicit_opt_in(collector):
    """#239 review: a reachable collector and the OTel SDK must not, alone, ship document text out."""
    pytest.importorskip("opentelemetry.sdk.trace")
    assert _names() == ["sql"]


@pytest.mark.parametrize("sinks", ["", "sql", "langfuse"])
def test_phoenix_requires_naming_it(collector, monkeypatch, sinks):
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", sinks)
    assert "phoenix" not in _names()


def test_phoenix_skipped_when_unreachable(monkeypatch):
    from audit.sinks.phoenix import PROBE_TIMEOUT_S

    monkeypatch.setenv("PHOENIX_TRACING", "enabled")
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", "phoenix")
    assert _names() == ["sql"]
    assert PROBE_TIMEOUT_S == 1.0


def test_phoenix_skipped_on_railway_localhost_endpoint(collector, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", "phoenix")
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


def test_entry_point_sink_loaded(monkeypatch, full_trace):
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
    assert "prompt" not in v and "completion" not in v and v["trace_sink"] is None  # off: no sink stores the text


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


def test_no_sink_stores_text_when_full_trace_is_off(monkeypatch):
    """#239 review (supersedes T22): ``audit.full_trace: false`` keeps document text out of the row
    *and* out of ``audit_trace_io`` and every other sink; the digest and the metadata remain."""
    from datetime import datetime, timezone

    from audit.digests import io_sha256
    from audit.sinks.sql import SqlSink
    from audit.trace import visit

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    monkeypatch.setenv("MAILROOM_TRACE_SINKS", "sql,langfuse,otlp,braintrust")
    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "0")
    seen = []
    monkeypatch.setattr("audit.sinks._entry_points", lambda: [_EntryPoint("memory", type(
        "Spy", (_MemorySink,), {"record_io": lambda self, *a: seen.append(a) or "ref"}))])
    now = datetime.now(timezone.utc)
    v = visit("classify", {"doc_id": "d-pii"}, {}, started_at=now, ended_at=now,
              llm={"prompt": "Jane Doe, SSN 000-00-0000", "completion": "ok", "model": "m", "prompt_tokens": 7})
    assert "prompt" not in v and "completion" not in v  # not in the row ...
    assert SqlSink().fetch_io(v["visit_id"]) is None  # ... nor in audit_trace_io ...
    assert seen == []  # ... nor handed to any other sink (built-in or entry point)
    assert v["io_sha256"] == io_sha256("Jane Doe, SSN 000-00-0000", "ok") and v["trace_sink"] is None
    assert (v["model"], v["prompt_tokens"]) == ("m", 7)  # metadata is still recorded


def test_sql_sink_stores_io_when_full_trace_is_on(monkeypatch, full_trace):
    from datetime import datetime, timezone

    from audit.sinks.sql import SqlSink
    from audit.trace import visit

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    now = datetime.now(timezone.utc)
    v = visit("classify", {"doc_id": "d"}, {}, started_at=now, ended_at=now,
              llm={"prompt": "the prompt", "completion": "ok", "model": "m"})
    stored = SqlSink().fetch_io(v["visit_id"])
    assert stored["prompt"] == "the prompt" and stored["sha256"] == v["io_sha256"]


def test_entry_points_are_scanned_once_per_process(monkeypatch):
    """#239 review: ``active_sinks`` runs on every node visit; the entry-point scan must not."""
    import importlib.metadata

    from audit import sinks

    sinks._scan_entry_points.cache_clear()
    calls = []
    real = importlib.metadata.entry_points
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **kw: calls.append(kw) or real(**kw))
    try:
        for _ in range(5):
            sinks._scan_entry_points()
        assert len(calls) == 1
    finally:
        sinks._scan_entry_points.cache_clear()


def test_one_malformed_spool_row_does_not_block_the_valid_ones(monkeypatch, tmp_path):
    """Review thread T21: ``audit-reconcile`` reads the spool through ``fetch_pending``; one bad row must not hide the rest."""
    import sqlite3

    from audit.sinks import capture_pending
    from audit.sinks.sql import SqlSink

    monkeypatch.setenv("PHOENIX_TRACING", "disabled")
    monkeypatch.setenv("MAILROOM_AUDIT_SPOOL_PATH", str(tmp_path / "spool.db"))
    capture_pending({"doc_id": "d-1", "event": "archived"})
    with sqlite3.connect(tmp_path / "spool.db") as conn:
        conn.execute("INSERT INTO pending_rows (ref, doc_id, record, created_at) VALUES "
                     "('pend-bad', 'd-bad', '{\"truncated\": ', '2000-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO pending_rows (ref, doc_id, record, created_at) VALUES "
                     "('pend-list', 'd-list', '[1, 2]', '2000-01-01T00:00:01+00:00')")
    capture_pending({"doc_id": "d-2", "event": "archived"})

    pending = SqlSink().fetch_pending()
    assert sorted(p["doc_id"] for p in pending) == ["d-1", "d-2"]
    assert all(p["ref"].startswith("pend-") and p["ref"] not in ("pend-bad", "pend-list") for p in pending)


class _Recording:
    """A span that records, or not, like the SDK's or the API's no-op one."""

    def __init__(self, recording):
        self._recording = recording
        self.attributes = {}

    def is_recording(self):
        return self._recording

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def get_span_context(self):
        from opentelemetry import trace

        # A no-op tracer can still propagate a *valid* parent context, so validity proves nothing.
        return trace.SpanContext(trace_id=0xABC, span_id=0xDEF, is_remote=False)


class _Tracer:
    def __init__(self, recording):
        self.span = _Recording(recording)

    def start_as_current_span(self, name):
        import contextlib

        return contextlib.nullcontext(self.span)


@pytest.mark.parametrize("module, cls", [("otlp", "OtlpSink"), ("phoenix", "PhoenixSink")])
@pytest.mark.parametrize("recording, kept", [(False, False), (True, True)])
def test_otel_sinks_keep_a_reference_only_for_a_recording_span(monkeypatch, module, cls, recording, kept):
    """Review thread T20: a span nobody records (no provider, failed init) stored nothing, so it has no reference."""
    pytest.importorskip("opentelemetry.trace")
    import importlib

    mod = importlib.import_module(f"audit.sinks.{module}")
    tracer = _Tracer(recording)
    monkeypatch.setattr(mod, "_tracer", lambda: tracer)
    sink = getattr(mod, cls)()

    io_ref = sink.record_io("v-1", "d-1", "prompt", "completion")
    pending_ref = sink.capture_pending({"doc_id": "d-1", "event": "archived"})

    assert (io_ref is not None, pending_ref is not None) == (kept, kept)
    if kept:
        assert io_ref == format(0xDEF, "016x")


@pytest.mark.parametrize("name", ["braintrust", "langfuse", "otlp", "phoenix"])
def test_write_only_sinks_are_not_recoverable(name):
    """Review thread T19: these sinks always return ``[]`` from ``fetch_pending``, so ``audit-reconcile`` cannot read them back."""
    import importlib

    cls = {"braintrust": "BraintrustSink", "langfuse": "LangfuseSink", "otlp": "OtlpSink", "phoenix": "PhoenixSink"}[name]
    sink = getattr(importlib.import_module(f"audit.sinks.{name}"), cls)()
    assert sink.fetch_pending() == []
    assert sink.recoverable is False


def test_capture_pending_marks_a_copy_only_a_recoverable_sink_can_return_as_complete(monkeypatch):
    """Review thread T19: a write-only sink keeps a locating reference, never a complete recoverable copy."""
    from audit import sinks

    class WriteOnly:
        name = "write-only"
        recoverable = False

        def capture_pending(self, record):
            return "span-1"

    class ReadBack:
        name = "read-back"  # no ``recoverable`` attribute: a third-party sink that implements ``fetch_pending``

        def capture_pending(self, record):
            return "kept-1"

    monkeypatch.setattr(sinks, "active_sinks", lambda: [WriteOnly(), ReadBack()])
    kept = sinks.capture_pending({"doc_id": "d-1"})
    assert [(k["sink"], k["complete"], k["recoverable"]) for k in kept] == [
        ("write-only", False, False), ("read-back", True, True)]


class _FakeLangfuse:
    """A Langfuse client double: ``trace_id`` is what ``get_current_trace_id`` returns."""

    def __init__(self, trace_id):
        self.trace_id, self.events = trace_id, []

    def get_current_trace_id(self):
        return self.trace_id

    def create_event(self, **kwargs):
        self.events.append(kwargs)


@pytest.mark.parametrize("trace_id, created", [(None, False), ("abc123", True)])
def test_langfuse_sink_never_opens_an_untagged_root_trace(monkeypatch, trace_id, created):
    """#239 review: an event created with no active observation opens a new root trace without tags."""
    from audit.sinks import langfuse as lf

    client = _FakeLangfuse(trace_id)
    monkeypatch.setattr(lf, "_client", lambda: client)
    sink = lf.LangfuseSink()
    io_ref = sink.record_io("v-1", "d-1", "p", "c")
    pending_ref = sink.capture_pending({"doc_id": "d-1", "event": "archived"})
    assert (io_ref is not None, pending_ref is not None) == (created, created)
    assert [e["name"] for e in client.events] == (["audit-io", "audit-pending-row"] if created else [])
