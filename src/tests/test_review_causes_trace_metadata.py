"""#217: ``review_causes`` (incl. ``transient_exhausted``) ride on the Langfuse trace metadata.

A document routed to review used to carry its causes only in the review manifest, the
``human_review`` interrupt payload and the parked state, so a trace-driven view could not
say why it went to review. ``human_review_node`` now writes them as trace metadata.
"""

from __future__ import annotations

import json

import pytest


class _Stop(Exception):
    """Raised by a patched ``interrupt`` so the node test stops where it parks."""


def _park(state, mocker):
    from graph.build_graph import human_review_node

    seen: list[dict] = []
    mocker.patch(
        "observability.tracing.set_trace_metadata", side_effect=lambda md: seen.append(dict(md))
    )
    mocker.patch("graph.build_graph.interrupt", side_effect=_Stop)
    with pytest.raises(_Stop):
        human_review_node(state)
    return seen


def test_transient_exhausted_reaches_the_trace_metadata(mocker):
    state = {
        "doc_id": "d-217",
        "matter_id": "M",
        "doc_type": "contract",
        "transient_error": True,
        "transient_retries_extract": 5,
    }
    seen = _park(state, mocker)
    assert len(seen) == 1
    assert "transient_exhausted" in seen[0]["review_causes"]


def test_trace_metadata_matches_the_interrupt_payload_causes(mocker):
    from graph.build_graph import human_review_node

    payloads: list[dict] = []
    metadata: list[dict] = []
    mocker.patch("observability.tracing.set_trace_metadata", side_effect=lambda md: metadata.append(dict(md)))
    mocker.patch("graph.build_graph.interrupt", side_effect=lambda p: payloads.append(p) or (_ for _ in ()).throw(_Stop()))
    state = {"doc_id": "d-217b", "matter_id": "M", "doc_type": "unknown", "doc_text": "hello"}
    with pytest.raises(_Stop):
        human_review_node(state)
    assert payloads[0]["review_causes"]  # a cause was recorded
    assert metadata[0]["review_causes"] == payloads[0]["review_causes"]


def test_a_tracing_failure_never_blocks_the_park(mocker):
    from graph.build_graph import human_review_node

    mocker.patch("observability.tracing.set_trace_metadata", side_effect=RuntimeError("langfuse down"))
    mocker.patch("graph.build_graph.interrupt", side_effect=_Stop)
    with pytest.raises(_Stop):  # still reaches interrupt(): the document is parked
        human_review_node({"doc_id": "d-217c", "matter_id": "M", "doc_type": "unknown"})


def test_langfuse_backend_writes_trace_and_observation_metadata(monkeypatch):
    """The active span carries ``review_causes`` as a JSON list under the trace metadata key."""
    from opentelemetry.sdk.trace import TracerProvider

    from observability import langfuse_setup

    monkeypatch.setattr(langfuse_setup, "get_langfuse_client", lambda: object())
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("route-for-review") as span:
        langfuse_setup.set_current_trace_metadata({"review_causes": ["transient_exhausted", "boss_conflict"]})
        attrs = dict(span.attributes)
    assert json.loads(attrs["langfuse.trace.metadata.review_causes"]) == ["transient_exhausted", "boss_conflict"]
    assert json.loads(attrs["langfuse.observation.metadata.review_causes"]) == ["transient_exhausted", "boss_conflict"]


def test_noop_backend_writes_nothing(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider

    from observability import langfuse_setup

    monkeypatch.setattr(langfuse_setup, "get_langfuse_client", lambda: langfuse_setup._NoopLangfuse())
    with TracerProvider().get_tracer("test").start_as_current_span("x") as span:
        langfuse_setup.set_current_trace_metadata({"review_causes": ["a"]})
        assert not any(k.startswith("langfuse.") for k in span.attributes)


def test_tracing_facade_is_a_noop_off_langfuse(monkeypatch):
    from observability import tracing

    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    tracing.set_trace_metadata({"review_causes": ["a"]})  # must not raise
