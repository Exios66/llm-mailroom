"""Triage-lane trace tagging and flushing (plan Task 17, Review Focus 3)."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from pipeline import watcher


def test_triage_trace_carries_mailroom_and_env_tags(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_ENVIRONMENT", "pilot")
    kw = watcher._triage_trace_kwargs(Path("/inbox/a.pdf"), {"source": "gmail", "route": "triage"})
    assert kw["tags"] == ["mailroom", "pilot", "source-gmail", "route-triage"]
    assert kw["environment"] == "pilot"
    assert kw["name"] == "gmail-triage"


def test_triage_trace_env_defaults_to_live(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("LANGFUSE_TRACING_ENVIRONMENT", raising=False)
    kw = watcher._triage_trace_kwargs(Path("a.pdf"), {})
    assert kw["environment"] == "live" and "live" in kw["tags"]


def test_triage_trace_input_has_no_document_text(tmp_path):
    f = tmp_path / "claim.txt"
    f.write_text("SECRET BODY alice@example.com")
    kw = watcher._triage_trace_kwargs(f, {"source": "gmail", "sender": "alice@example.com"})
    assert kw["input"] == {"filename": "claim.txt", "size_bytes": f.stat().st_size}
    assert "SECRET" not in repr(kw)


def test_triage_lane_flushes_on_failure(monkeypatch, tmp_path):
    calls = []

    @contextmanager
    def fake_trace(**kw):
        calls.append(("trace", kw["tags"]))
        yield None

    monkeypatch.setattr(watcher, "pipeline_trace", fake_trace)
    monkeypatch.setattr("observability.tracing.flush", lambda: calls.append(("flush",)))

    def boom(*a, **k):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(watcher, "_run_triage_lane", boom)
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(RuntimeError):
        watcher._run_triage_traced(f, "M-1", {"source": "gmail"})
    assert calls[0][0] == "trace" and "mailroom" in calls[0][1]
    assert ("flush",) in calls
