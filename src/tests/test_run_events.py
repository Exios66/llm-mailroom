"""Run journals record exact node ingress without leaking payloads."""

from __future__ import annotations

import json
from pathlib import Path

from graph.run_events import RunEventRecorder


def test_journal_records_real_node_runs_only(tmp_path: Path):
    recorder = RunEventRecorder(
        document={"filename": "doc.txt", "matter_id": "M-1", "source": "live-manifest"},
        ground_truth={"expected_doc_class": "contract", "expected_stage": "archived"},
        runs_dir=tmp_path,
    )
    recorder.on_chain_start(
        None, {}, run_id="a", tags=["graph:step:1"], metadata={"langgraph_node": "intake"}
    )
    # Nested sequencing chain for the same node: must be ignored.
    recorder.on_chain_start(
        None, {}, run_id="a-seq", tags=["seq:step:1"], metadata={"langgraph_node": "intake"}
    )
    recorder.on_chain_end({"stage": "inbox", "doc_text": "SECRET"}, run_id="a")
    recorder.on_chain_start(
        None, {}, run_id="b", tags=["graph:step:2"], metadata={"langgraph_node": "classify"}
    )
    recorder.on_chain_error(ValueError("C:/private/path"), run_id="b")
    recorder.on_chain_start(
        None, {}, run_id="start", tags=["graph:step:0"], metadata={"langgraph_node": "__start__"}
    )

    payload = recorder.finalize({"stage": "archived", "doc_type": "contract", "doc_id": "doc-1"})

    assert payload["node_sequence"] == ["intake", "classify"]
    assert payload["transitions"] == [{"source": "intake", "target": "classify"}]
    assert payload["outcome"]["stage"] == "archived"
    assert payload["document"]["doc_id"] == "doc-1"
    completed = next(event for event in payload["events"] if event["type"] == "node_completed")
    assert completed["output"] == {"stage": "inbox"}
    serialized = json.dumps(payload)
    assert "SECRET" not in serialized
    assert "private" not in serialized
    assert (tmp_path / f"doc-1-{recorder.execution_id[:12]}.json").is_file()


def test_finalize_is_idempotent_for_readers(tmp_path: Path):
    recorder = RunEventRecorder(document={"filename": "doc.txt"}, runs_dir=tmp_path)
    recorder.on_chain_start(
        None, {}, run_id="a", tags=["graph:step:1"], metadata={"langgraph_node": "intake"}
    )
    first = recorder.finalize({"stage": "failed", "doc_id": "d"})
    second = recorder.finalize({"stage": "failed", "doc_id": "d"})
    assert first["events"] == second["events"]
    assert second["outcome"]["failed"] is True
