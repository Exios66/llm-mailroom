"""HITL-1: a human's decision flows back into the graph as first-class data.

Covers approve_as_is / correct (reviewer fields through compile_report ->
catalog_write -> archive with NO LLM call), reject checkpoint hygiene, the
sorter-stage class-confirm, the restart fallback, the per-document resolve lock
(409), the atomic manifest write and the human_review_node UnboundLocalError.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

H = {"Authorization": "Bearer tok"}


def _resp(content):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


@pytest.fixture
def llm(mocker, mock_langchain_llm):
    mock_langchain_llm.classification = {
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.40, "reasoning": "Unsure",
    }
    mock_langchain_llm.extraction = {
        "parties": ["Acme Corp"], "effective_date": "2024-01-01", "confidence": 0.91,
    }
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _resp('{"label": "incomplete", "score": 0.2, "findings": ["gap"], "reasoning": "gap"}'),
        _resp('{"decision": "human_review", "reasoning": "gap", "fields_to_fix": [], "handoff_notes": "n"}'),
    ] * 3
    mocker.patch("llm.client.OpenAI", return_value=client)
    mocker.patch(
        "agents.base.BaseAgent.__init__",
        lambda self, mock=client: setattr(self, "client", mock) or setattr(self, "model", "test-model"),
    )
    return client


@pytest.fixture
def api(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "tok")
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    if "api.main" in sys.modules:
        importlib.reload(sys.modules["api.main"])
    from api.main import app

    with TestClient(app) as c:
        yield c


def _sorter_park(temp_base_dir, name):
    from graph.build_graph import run_pipeline

    f = temp_base_dir / "pipeline" / "inbox" / name
    f.write_text("Service agreement between Acme Corp and Beta LLC.")
    result = run_pipeline(f, "M-HITL")
    assert result["stage"] == "review"
    return result["doc_id"]


def _extraction_park(temp_base_dir, name):
    """Park AFTER extraction: the judge says incomplete, the arbiter sends it to a human."""
    from graph.build_graph import resume_from_review
    from pipeline.bins import load_manifest, review_file_path

    doc_id = _sorter_park(temp_base_dir, name)
    manifest = load_manifest(doc_id)
    resumed = resume_from_review(manifest, review_file_path(manifest))
    assert resumed["stage"] == "review"
    parked = load_manifest(doc_id)
    assert parked.extracted_data["parties"] == ["Acme Corp"]
    return doc_id


def _forbid_llm(mocker, client):
    from langchain_agents.base_agent import BaseAgent as LCBase

    def boom(*a, **k):
        raise AssertionError("LLM call after a human resolution")

    client.chat.completions.create.side_effect = boom
    mocker.patch.object(LCBase, "llm", new=boom)


def _audit(doc_id):
    from storage.audit_log import get_audit_chain

    return asyncio.run(get_audit_chain(doc_id))


def _events(doc_id):
    return [getattr(e, "event", None) or e.get("event") for e in _audit(doc_id)]


def _interrupted(doc_id):
    from graph.build_graph import _thread_is_interrupted, get_compiled_graph
    from pipeline.bins import load_manifest

    thread = load_manifest(doc_id).checkpoint_thread_id
    return _thread_is_interrupted(get_compiled_graph(), thread)


def _resolve(api, doc_id, **body):
    return api.post(f"/review/{doc_id}/resolve", headers=H, json=body)


def test_approve_as_is_archives_through_tail_without_llm(api, temp_base_dir, llm, mocker):
    from pipeline.bins import load_manifest
    from storage.catalog import lookup_document

    doc_id = _extraction_park(temp_base_dir, "a.txt")
    assert _interrupted(doc_id)
    _forbid_llm(mocker, llm)
    r = _resolve(api, doc_id, resolution="approve_as_is", reviewer="pat", notes="ok")
    assert r.status_code == 200, r.text
    assert r.json()["resolution"] == "approve_as_is"
    archive_path = r.json()["complete"]["archive_path"]  # legacy response key kept
    assert archive_path and pathlib.Path(archive_path).exists()
    m = load_manifest(doc_id)
    assert m.stage.value == "archived"
    assert m.extraction_source == "human"
    assert m.extracted_data["parties"] == ["Acme Corp"]
    assert "_report" in m.extracted_data  # compile_report ran
    assert m.extraction_confidence == 0.91  # the model's number, not a fabricated 1.0
    label = m.human_label
    assert label["resolution"] == "approve_as_is" and label["reviewer"] == "pat"
    assert label["model_snapshot"]["extracted_data"]["parties"] == ["Acme Corp"]
    assert label["model_snapshot"]["extraction_confidence"] == 0.91
    events = _events(doc_id)
    assert "review_resolved" in events and "archived" in events
    assert events.index("review_resolved") < events.index("archived")
    assert asyncio.run(lookup_document(doc_id=doc_id)).stage == "archived"
    assert not _interrupted(doc_id)


def test_correct_merges_fields_and_invalid_correction_stays_in_review(api, temp_base_dir, llm, mocker):
    from pipeline.bins import load_manifest

    doc_id = _extraction_park(temp_base_dir, "c.txt")
    _forbid_llm(mocker, llm)
    bad = _resolve(api, doc_id, resolution="correct", field_corrections={"parties": 5})
    assert bad.status_code == 400, bad.text
    assert load_manifest(doc_id).stage.value == "review"
    assert _interrupted(doc_id)

    ok = _resolve(
        api, doc_id, resolution="correct", field_corrections={"parties": ["Acme Corp", "Beta LLC"]}
    )
    assert ok.status_code == 200, ok.text
    m = load_manifest(doc_id)
    assert m.stage.value == "archived" and m.extraction_source == "human"
    assert m.extracted_data["parties"] == ["Acme Corp", "Beta LLC"]
    assert m.extracted_data["effective_date"] == "2024-01-01"  # merged over, not replaced
    assert m.human_label["field_corrections"] == {"parties": ["Acme Corp", "Beta LLC"]}
    assert m.human_label["model_snapshot"]["extracted_data"]["parties"] == ["Acme Corp"]


def test_reject_resolves_checkpoint_and_persists_reason(api, temp_base_dir, llm):
    from pipeline.bins import failed_dir, load_manifest

    doc_id = _extraction_park(temp_base_dir, "r.txt")
    assert _interrupted(doc_id)
    r = _resolve(api, doc_id, resolution="reject", reject_reason="spam", notes="not ours")
    assert r.status_code == 200, r.text
    m = load_manifest(doc_id)
    assert m.stage.value == "failed" and m.reject_reason == "spam"
    assert m.human_label["resolution"] == "reject"
    assert any(p.is_file() for p in failed_dir().iterdir())
    assert not _interrupted(doc_id)
    assert "review_resolved" in _events(doc_id) and "review_rejected" in _events(doc_id)
    assert _resolve(api, doc_id, resolution="reject", reject_reason="bogus").status_code in (400, 409)


def test_sorter_stage_approve_as_is_reenters_extract(api, temp_base_dir, llm):
    from pipeline.bins import load_manifest

    doc_id = _sorter_park(temp_base_dir, "s.txt")
    assert load_manifest(doc_id).extracted_data is None
    llm.chat.completions.create.side_effect = None
    r = _resolve(api, doc_id, resolution="approve_as_is")
    assert r.status_code == 200, r.text
    assert "resume" in r.json()  # re-extract route, not the no-LLM tail
    assert load_manifest(doc_id).human_label["resolution"] == "approve_as_is"


@pytest.mark.parametrize("body", [
    {"resolution": "approve_as_is"},
    {"resolution": "correct", "field_corrections": {"parties": ["Z Corp"]}},
])
def test_restart_fallback_reaches_same_tail(api, temp_base_dir, llm, mocker, body):
    from graph.build_graph import reset_compiled_graph
    from pipeline.bins import load_manifest

    doc_id = _extraction_park(temp_base_dir, "f.txt")
    reset_compiled_graph()  # the MemorySaver is lost, as after a restart
    assert not _interrupted(doc_id)
    _forbid_llm(mocker, llm)
    r = _resolve(api, doc_id, **body)
    assert r.status_code == 200, r.text
    m = load_manifest(doc_id)
    assert m.stage.value == "archived" and m.extraction_source == "human"
    assert "_report" in m.extracted_data
    events = _events(doc_id)
    assert events.index("review_resolved") < events.index("archived")


def test_concurrent_double_approve_archives_once(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "d.txt")
    _forbid_llm(mocker, llm)
    out = []

    def go():
        out.append(TestClient(api.app).post(
            f"/review/{doc_id}/resolve", headers=H, json={"resolution": "approve_as_is"}
        ))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes = sorted(r.status_code for r in out)
    # The loser either collides with the held lock (409) or arrives after the
    # winner archived the document (legacy 400, stage named).
    assert codes in ([200, 400], [200, 409]), [r.text for r in out]
    loser = next(r for r in out if r.status_code != 200)
    assert "PipelineStage" not in loser.text
    assert _events(doc_id).count("archived") == 1
    late = _resolve(api, doc_id, resolution="approve_as_is")
    assert late.status_code == 400 and "archived" in late.text and "PipelineStage" not in late.text


def test_legacy_requests_keep_their_meaning(api, temp_base_dir, llm):
    from pipeline.bins import load_manifest

    doc_id = _extraction_park(temp_base_dir, "l.txt")
    llm.chat.completions.create.side_effect = None
    r = _resolve(api, doc_id, decision="approved", disposition="complete",
                 extracted_data={"parties": ["Legacy Co"], "effective_date": "2024-02-02"})
    assert r.status_code == 200, r.text
    assert r.json()["complete"]["stage"] == "archived"
    m = load_manifest(doc_id)
    assert m.extraction_source == "human" and m.human_label["resolution"] == "correct"
    assert m.human_label["legacy_request"] is True


def test_human_review_node_without_file_path_is_not_unbound(mocker):
    from graph.build_graph import human_review_node

    class InterruptReached(Exception):
        pass

    # A sentinel proves the node got as far as interrupt(); any earlier error
    # (UnboundLocalError included) would escape as a different exception.
    interrupt = mocker.patch("graph.build_graph.interrupt", side_effect=InterruptReached)
    with pytest.raises(InterruptReached):
        human_review_node({"doc_id": "x", "matter_id": "M"})
    interrupt.assert_called_once()
    payload = interrupt.call_args.args[0]
    assert payload["action"] == "human_review"
    assert payload["doc_id"] == "x"
    assert payload["matter_id"] == "M"


def test_save_manifest_is_atomic(temp_base_dir, monkeypatch):
    from pathlib import Path

    from pipeline.bins import manifests_dir, save_manifest
    from schemas.manifest import DocumentManifest

    m = DocumentManifest(doc_id="atomic-1", matter_id="M", original_filename="x.txt")
    path = save_manifest(m)
    before = path.read_text()
    real = Path.write_text

    def partial(self, data, *a, **k):
        real(self, data[: len(data) // 2], *a, **k)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", partial)
    m.matter_id = "CHANGED"
    with pytest.raises(OSError):
        save_manifest(m)
    monkeypatch.undo()
    assert path.read_text() == before
    assert not [p for p in manifests_dir().iterdir() if p.suffix == ".tmp"]
