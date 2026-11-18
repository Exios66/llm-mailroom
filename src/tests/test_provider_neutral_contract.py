"""The local contract survives every trace adapter and rejects semantic drift."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import subprocess
import sys

import pytest


def test_core_import_is_isolated_and_immutable():
    from pathlib import Path
    src = str(Path(__file__).resolve().parents[1])
    code = '''
import builtins, sys
sys.path.insert(0, SOURCE)
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"langfuse", "braintrust", "phoenix", "opentelemetry", "graph", "pipeline", "llm_dojo_scoring"}:
        raise AssertionError("forbidden import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import mailroom_contract as contract
assert len(contract.NODE_BINDINGS) == 22
for operation in (
    lambda: contract.NODE_BINDINGS.__setitem__("classify", ("other", "agent")),
    lambda: contract.OBSERVATION_BINDINGS.__setitem__("classify-document", "span"),
    lambda: contract.SCORE_CONFIGS[0].__setitem__("name", "other"),
    lambda: contract.SCORE_BY_NAME["completeness_label"]["categories"][0].__setitem__("label", "other"),
):
    try:
        operation()
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("contract is mutable")
'''.replace("SOURCE", repr(src))
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_core_rejects_invalid_bindings_and_scores_without_a_sink():
    from mailroom_contract import validate_node_binding, validate_observation, validate_score

    validate_node_binding("classify", "classify-document", "agent")
    for args in (("classify", "extract-fields", "agent"),
                 ("classify", "classify-document", "evaluator"),
                 ("unknown", "classify-document", "agent")):
        with pytest.raises(ValueError):
            validate_node_binding(*args)
    with pytest.raises(ValueError):
        validate_observation("ad-hoc", "mystery")
    with pytest.raises(ValueError):
        validate_observation("classify-document", "span")
    assert validate_observation("ad-hoc", "span") == "span"
    validate_score("deterministic_verdict", "PARTIAL", "CATEGORICAL")
    for name, value, kind in (("class_correct", 2, None),
                              ("extraction_f1", float("nan"), None),
                              ("extraction_f1", 0.5, "BOOLEAN"),
                              ("no_such_score", 1, None)):
        with pytest.raises(ValueError):
            validate_score(name, value, kind)


def test_derived_score_validates_with_no_sink_and_delivers_with_braintrust(monkeypatch):
    from observability import scores

    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    scores.create_trace_score("trace-1", "deterministic_verdict", "PARTIAL", data_type="CATEGORICAL")
    with pytest.raises(ValueError):
        scores.create_trace_score("trace-1", "deterministic_verdict", "UNKNOWN")
    calls = []
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "braintrust")
    monkeypatch.setitem(sys.modules, "braintrust", SimpleNamespace(
        current_span=lambda: SimpleNamespace(log=lambda **kwargs: calls.append(kwargs))))
    scores.score_trace("deterministic_verdict", "CORRECT", data_type="CATEGORICAL")
    scores.score_trace("judge_notes", "checked", data_type="TEXT")
    assert calls == [
        {"scores": {"deterministic_verdict": 1.0},
         "metadata": {"mailroom.score.deterministic_verdict": "CORRECT",
                      "mailroom.score_type.deterministic_verdict": "CATEGORICAL"}},
        {"metadata": {"mailroom.score.judge_notes": "checked",
                      "mailroom.score_type.judge_notes": "TEXT"}},
    ]


@pytest.mark.parametrize("provider", ["none", "langfuse", "braintrust", "phoenix"])
def test_production_emitters_validate_and_deliver_same_scores(monkeypatch, provider):
    from observability import scores

    monkeypatch.setenv("OBSERVABILITY_PROVIDER", provider)
    delivered = []
    if provider == "langfuse":
        monkeypatch.setattr(scores, "_client", lambda: SimpleNamespace(
            score_current_trace=lambda **kwargs: delivered.append((kwargs["name"], kwargs["value"]))))
    elif provider == "braintrust":
        monkeypatch.setitem(sys.modules, "braintrust", SimpleNamespace(
            current_span=lambda: SimpleNamespace(log=lambda **kwargs:
                delivered.extend(kwargs.get("scores", {}).items()))))
    elif provider == "phoenix":
        import opentelemetry.trace as otel_trace
        monkeypatch.setattr(otel_trace, "get_current_span", lambda: SimpleNamespace(
            set_attribute=lambda key, value: delivered.append((key, value))))

    run = scores.emit_pipeline_scores({}, precomputed={"stage_completed": 1})
    block = scores.emit_scoring_scores({"extraction_f1": 0.5}, None)
    assert run == {"stage_completed": 1}
    assert block == {"extraction_f1": 0.5}
    if provider == "none":
        assert delivered == []
    elif provider == "phoenix":
        assert delivered == [("mailroom.score.stage_completed", 1),
                             ("mailroom.score.extraction_f1", 0.5)]
    else:
        assert delivered == [("stage_completed", 1.0 if provider == "braintrust" else 1),
                             ("extraction_f1", 0.5)]
    with pytest.raises(ValueError, match="unknown Mailroom score"):
        scores.emit_pipeline_scores({}, precomputed={"unregistered": 1})


def test_runtime_graph_rejects_decorator_drift(monkeypatch):
    import graph.build_graph as bg
    from langgraph.checkpoint.memory import MemorySaver

    original = bg.traced_node

    def drift(name, **kwargs):
        if name == "classify-document":
            name = "extract-fields"
        return original(name, **kwargs)

    monkeypatch.setattr(bg, "traced_node", drift)
    with pytest.raises(ValueError, match="graph node 'classify' must bind"):
        bg.build_graph(checkpointer=MemorySaver())


class FakeSpan:
    def __init__(self, name, records, fail_update=False):
        self.name, self.records, self.fail_update = name, records, fail_update
        self.id = "fake-id"

    def update(self, **kwargs):
        self.records.append(("update", self.name, kwargs))
        if self.fail_update:
            raise RuntimeError("sink update failed")
        return self

    def log(self, **kwargs):
        return self.update(**kwargs)

    def __enter__(self):
        self.records.append(("enter", self.name))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.records.append(("exit", self.name, exc_type.__name__ if exc_type else None))


@pytest.mark.parametrize("provider", ["none", "langfuse", "braintrust", "phoenix"])
def test_identical_node_body_and_error_under_each_adapter(monkeypatch, provider):
    from observability import tracing

    records = []
    monkeypatch.setattr(tracing, "resolve_provider_name", lambda: provider)
    monkeypatch.setattr(tracing, "flush", lambda: None)
    if provider == "langfuse":
        from observability import langfuse_setup as backend
        monkeypatch.setattr(backend, "get_langfuse_client", lambda: SimpleNamespace(
            create_trace_id=lambda **kw: "trace-id",
            start_as_current_observation=lambda **kw: FakeSpan(kw["name"], records)))
        @contextmanager
        def propagate_attributes(**kwargs):
            yield
        monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(propagate_attributes=propagate_attributes))
    elif provider == "braintrust":
        from observability import braintrust_setup as backend
        monkeypatch.setattr(backend, "configure", lambda: True)
        monkeypatch.setattr(backend, "_is_braintrust_available", lambda: True)
        monkeypatch.setitem(sys.modules, "braintrust", SimpleNamespace(
            start_span=lambda **kw: (records.append(("native", kw["name"], kw["type"], kw["metadata"])) or FakeSpan(kw["name"], records))))
    elif provider == "phoenix":
        from observability import phoenix_setup as backend
        monkeypatch.setattr(backend, "_init_opentelemetry", lambda: SimpleNamespace(
            get_tracer=lambda name: SimpleNamespace(start_as_current_span=lambda span_name, attributes=None:
                (records.append(("native", span_name, attributes)) or OtelSpan(span_name, records)))))
        import opentelemetry.trace as otel_trace
        class OtelSpan(FakeSpan):
            def get_span_context(self):
                return SimpleNamespace(span_id=1)
            def set_attribute(self, key, value):
                records.append(("attr", self.name, key, value))
        monkeypatch.setattr(otel_trace, "get_tracer", lambda name: pytest.fail("foreign global OTel provider used"))

    calls = []
    @tracing.traced_node("classify-document")
    def node(state):
        calls.append("called")
        if state.get("pause"):
            from langgraph.errors import GraphInterrupt
            raise GraphInterrupt()
        if state.get("raise"):
            raise RuntimeError("node failed")
        return {"stage": "classified"}

    with tracing.pipeline_trace(name="document-pipeline", metadata={"source": "test"}):
        result = node({"doc_id": "d1", "stage": "ingested"})
        assert result["stage"] == "classified"
        assert len(result["audit_trace"]) == 1
        with pytest.raises(RuntimeError, match="node failed"):
            node({"doc_id": "d1", "stage": "ingested", "raise": True})
        with tracing.collect_unapplied_visits() as visits:
            from langgraph.errors import GraphInterrupt
            with pytest.raises(GraphInterrupt):
                node({"doc_id": "d1", "stage": "review", "pause": True})
        assert len(visits) == 1 and visits[0]["status"] == "paused"
    assert calls == ["called", "called", "called"]
    if provider != "none":
        assert ("enter", "document-pipeline") in records
        assert ("enter", "classify-document") in records
        assert any(row[:2] == ("exit", "classify-document") and row[2] == "RuntimeError" for row in records)
    if provider == "braintrust":
        assert ("native", "classify-document", "task", {"mailroom.semantic_type": "agent"}) in records
    if provider == "phoenix":
        assert ("native", "classify-document", {
            "mailroom.semantic_type": "agent", "openinference.span.kind": "AGENT"}) in records


@pytest.mark.parametrize("failure", ["enter", "update", "exit"])
def test_backend_failure_never_replays_node(monkeypatch, failure):
    from observability import tracing, langfuse_setup
    records = []
    class Broken(FakeSpan):
        def __enter__(self):
            if failure == "enter":
                raise RuntimeError("sink enter failed")
            return super().__enter__()
        def __exit__(self, *args):
            super().__exit__(*args)
            if failure == "exit":
                raise RuntimeError("sink exit failed")
    monkeypatch.setattr(tracing, "resolve_provider_name", lambda: "langfuse")
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(langfuse_setup, "observation", lambda name, **kw: Broken(name, records, fail_update=failure == "update"))
    calls = []
    @tracing.traced_node("classify-document")
    def node(state):
        calls.append(1)
        return {"stage": "classified"}
    result = node({"doc_id": "d1", "stage": "ingested"})
    assert len(calls) == 1
    assert result["stage"] == "classified"
    assert len(result["audit_trace"]) == 1
