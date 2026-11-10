"""Roadmap M5: node visits collected in ``state["audit_trace"]`` (spec §4.4, §5.2)."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import openai
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from graph import build_graph as bg
from graph.state import DocumentState

FIXTURE = Path(__file__).parent / "fixtures" / "audit" / "sandbox_decision_schema.json"
TEXT = "Service Agreement between Acme and Beta. Governing law: New York."
FIELDS = {"document_name": "Service Agreement", "parties": ["Acme", "Beta"], "governing_law": "New York"}


@pytest.fixture
def run_graph(temp_base_dir, monkeypatch, mocker):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MAILROOM_AUDIT_FULL_TRACE", raising=False)
    monkeypatch.setenv("MAILROOM_MEMORY_DIR", str(temp_base_dir / "memory"))
    sorter = mocker.patch("agents.sorter.SorterAgent.classify_json", return_value={
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "Agreement",
    })
    mocker.patch.object(bg, "_extract_contracts", side_effect=lambda *a: {**deepcopy(FIELDS), "confidence": 0.99})

    def run():
        source = temp_base_dir / "pipeline" / "inbox" / "agreement.txt"
        source.write_text(TEXT)
        graph = bg.build_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "trace-test"}}
        order = []
        for event in graph.stream({"file_path": str(source), "matter_id": "TRACE", "messages": []}, config,
                                  stream_mode="updates"):
            order.extend(event)
        return order, graph.get_state(config).values

    run.sorter = sorter
    return run


def test_traced_node_appends_one_visit_per_call(run_graph):
    order, final = run_graph()
    assert [v["node"] for v in final["audit_trace"]] == order
    assert len({v["visit_id"] for v in final["audit_trace"]}) == len(order)
    assert "trace_incomplete" not in final


def test_visit_fields_match_spec(run_graph, monkeypatch):
    from audit.trace import FULL_TRACE_KEYS, OUTCOMES, STATUSES, VISIT_KEYS

    _, final = run_graph()
    for v in final["audit_trace"]:
        assert tuple(v) == VISIT_KEYS
        assert v["status"] in STATUSES and v["outcome"] in OUTCOMES
        assert v["latency_ms"] >= 0 and v["ended_at"] >= v["started_at"]
    # The mocked agents record no LLM call, so nothing is filled in.
    classify = next(v for v in final["audit_trace"] if v["node"] == "classify")
    assert classify["model"] is None and classify["prompt_tokens"] is None and classify["io_sha256"] is None
    assert classify["decision"]["doc_type"] == "contract"
    assert classify["decision"]["audit_event"]["event"] == "classified"

    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "1")
    _, full = run_graph()
    for v in full["audit_trace"]:
        assert tuple(v) == VISIT_KEYS + FULL_TRACE_KEYS


def test_visit_reads_the_llm_calls_of_its_node(monkeypatch):
    from audit.digests import io_sha256
    from audit.trace import VISIT_KEYS
    from observability.tracing import traced_node
    from pipeline.limits import record_usage, reset_run_usage

    class Usage:
        prompt_tokens, completion_tokens = 120, 30

    reset_run_usage()
    record_usage(Usage(), "earlier-model", prompt="before", completion="the node")  # not this node's call

    @traced_node("classify-document")
    def classify_node(state):
        record_usage(Usage(), "test-model", served_model="served", prompt="p1", completion="c1", prompt_version=4)
        return {"doc_type": "contract", "classification_attempts": 1}

    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "0")
    (v,) = classify_node({"doc_id": "d-1", "audit_trace": [{"node": "intake"}]})["audit_trace"]
    assert tuple(v) == VISIT_KEYS
    assert (v["node"], v["model"], v["prompt_version"]) == ("classify", "served", 4)
    assert (v["prompt_tokens"], v["completion_tokens"]) == (120, 30)
    assert v["io_sha256"] == io_sha256("p1", "c1") and v["trace_sink"] is None and v["also_in"] == []
    monkeypatch.setenv("MAILROOM_AUDIT_FULL_TRACE", "1")
    (v,) = classify_node({"doc_id": "d-1", "audit_trace": [{"node": "intake"}]})["audit_trace"]
    assert (v["prompt"], v["completion"]) == ("p1", "c1")


def test_decisions_match_sandbox_schema(run_graph):
    from audit.trace import DECISION_KEYS, DECISION_TYPES, decisions_for

    schema = json.loads(FIXTURE.read_text())
    assert tuple(schema["keys"]) == DECISION_KEYS and tuple(schema["types"]) == DECISION_TYPES
    _, final = run_graph()
    decisions = [d for v in final["audit_trace"] for d in v["decisions"]]
    decisions += decisions_for("judge_verify", {}, {"judge_verdict": "pass", "judge_score": 0.9})
    decisions += decisions_for("intake", {}, {"bert_triage": {"doc_type": "contract", "confidence": 0.8}})
    assert {d["source"] for d in decisions} >= {"classify", "judge_verify", "bert_triage"}
    for d in decisions:
        assert list(d) == schema["keys"]
        assert d["type"] in schema["types"]
        for key in schema["string_keys"]:
            assert isinstance(d[key], str)
    (classified,) = [d for v in final["audit_trace"] if v["node"] == "classify" for d in v["decisions"]]
    assert (classified["answer"], classified["p"]) == ("contract", 0.99)
    assert "contract" in classified["options"]


def test_attempt_ids_follow_retry_counters(run_graph):
    from audit.trace import attempt_id_for

    success = deepcopy(run_graph.sorter.return_value)
    run_graph.sorter.side_effect = [openai.APIConnectionError(request=None), success]
    order, final = run_graph()
    assert order[:3] == ["intake", "classify", "classify"]
    intake, first, second, extract = final["audit_trace"][:4]
    doc = final["doc_id"]
    assert intake["attempt_id"] is None
    # A transient retry repeats the same attempt; its first visit is marked as a retry.
    assert first["attempt_id"] == second["attempt_id"] == f"att-{doc}-classification-r0-1"
    assert (first["outcome"], second["outcome"]) == ("retry", "ok")
    assert extract["attempt_id"] == f"att-{doc}-extraction-r0-1"
    state = {"doc_id": "d", "run_attempt": 2, "classification_attempts": 2, "extraction_attempts": 3}
    assert attempt_id_for(state, "retry_classify") == "att-d-classification-r2-2"
    assert attempt_id_for(state, "retry_extract") == "att-d-extraction-r2-3"
    assert attempt_id_for({**state, "judge_pass_count": 1}, "arbiter") == "att-d-judge-r2-1"
    assert attempt_id_for(state, "compile_report") is None


def test_intake_visit_carries_bert_triage(run_graph):
    from audit.trace import visit
    from datetime import datetime, timezone

    _, final = run_graph()
    intake = final["audit_trace"][0]
    assert intake["node"] == "intake"
    assert "bert_triage" in intake["decision"]
    assert intake["decision"]["file_sha256"] == final["file_sha256"]
    assert intake["decision"]["audit_event"] == {
        "event": "ingested", "actor": "pipeline",
        "detail": {"file_sha256": final["file_sha256"], "size_bytes": final["size_bytes"]},
    }
    now = datetime.now(timezone.utc)
    triage = {"doc_type": "contract", "subclass": "nda", "confidence": 0.93, "route": "fast_path"}
    v = visit("intake", {"doc_id": "d"}, {"bert_triage": triage}, started_at=now, ended_at=now)
    assert v["decision"]["bert_triage"] == triage
    assert v["decisions"][0]["source"] == "bert_triage" and v["decisions"][0]["answer"] == "contract"


def _two_step_graph(*, trace_first: bool):
    from observability.tracing import traced_node

    def intake_node(state):
        return {"doc_id": "d-resume", "stage": "processing"}

    def classify_node(state):
        return {"doc_type": "contract", "classification_attempts": 1}

    g = StateGraph(DocumentState)
    g.add_node("intake", traced_node("intake-document")(intake_node) if trace_first else intake_node)
    g.add_node("classify", traced_node("classify-document")(classify_node))
    g.add_edge(START, "intake")
    g.add_edge("intake", "classify")
    g.add_edge("classify", END)
    return g


def test_trace_survives_checkpoint_resume():
    saver = MemorySaver()
    graph = _two_step_graph(trace_first=True).compile(checkpointer=saver, interrupt_before=["classify"])
    config = {"configurable": {"thread_id": "resume"}}
    graph.invoke({"messages": []}, config)
    assert [v["node"] for v in graph.get_state(config).values["audit_trace"]] == ["intake"]
    # A new graph object on the same saver: the trace comes back from the checkpoint.
    resumed = _two_step_graph(trace_first=True).compile(checkpointer=saver, interrupt_before=["classify"])
    final = resumed.invoke(None, config)
    assert [v["node"] for v in final["audit_trace"]] == ["intake", "classify"]
    assert "trace_incomplete" not in final


def test_old_checkpoint_without_trace_resumes():
    """A checkpoint written before the trace existed (no traced intake) resumes with a partial trace."""
    saver = MemorySaver()
    old = _two_step_graph(trace_first=False).compile(checkpointer=saver, interrupt_before=["classify"])
    config = {"configurable": {"thread_id": "old"}}
    old.invoke({"messages": []}, config)
    assert not old.get_state(config).values.get("audit_trace")
    final = _two_step_graph(trace_first=True).compile(checkpointer=saver).invoke(None, config)
    assert [v["node"] for v in final["audit_trace"]] == ["classify"]
    assert final["trace_incomplete"] is True
    assert final["doc_type"] == "contract"


def test_no_stage_rows_written_during_run(run_graph):
    from storage.audit_log import get_audit_chain

    _, final = run_graph()
    events = [e["event"] for e in asyncio.run(get_audit_chain(final["doc_id"]))]
    # Only the terminal write is a row; the stage events are visits in the trace.
    assert events == ["archived"]
    assert not hasattr(bg, "_emit_stage_audit")
    noted = [v["decision"].get("audit_event", {}).get("event") for v in final["audit_trace"]]
    assert [e for e in noted if e] == ["ingested", "classified", "extracted"]


def test_record_visit_outside_a_node_is_dropped():
    from audit.trace import record_visit

    record_visit("orphan", detail={"x": 1})  # no traced node is running: nothing to attach to, no error
