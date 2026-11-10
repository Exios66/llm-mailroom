"""Behavioral contracts through the compiled production graph.

Only model responses are scripted. Routing, state propagation, guards,
reporting, filesystem bins, SQLite catalog and audit writes remain real.
"""
import asyncio
import hashlib
import json
import sqlite3
from copy import deepcopy

import openai
import pytest

from graph import build_graph as bg
from langgraph.checkpoint.memory import MemorySaver

RAW = "# **Service Agreement**\r\n\r\nAcme\u00a0  and **Beta** agree-\nment.\n\n> Governing law: `New York`.\n"
CLEAN = "Service Agreement\n\nAcme and Beta agreement.\n\nGoverning law: New York."
FIELDS = {"document_name": "Service Agreement", "parties": ["Acme", "Beta"],
          "governing_law": "New York", "contract_value": "$1,250.00"}


@pytest.fixture
def run_graph(temp_base_dir, monkeypatch, mocker):
    # Ignore a developer's external DB and isolate persistent agent memory.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAILROOM_MEMORY_DIR", str(temp_base_dir / "memory"))
    sorter = mocker.patch("agents.sorter.SorterAgent.classify_json", return_value={
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99,
        "reasoning": "Agreement between Acme and Beta",
    })
    extractor = mocker.patch.object(bg, "_extract_contracts", side_effect=lambda *a: {
        **deepcopy(FIELDS), "confidence": 0.99,
    })
    reporter = mocker.spy(__import__("agents.reporter", fromlist=["compile_matter_record"]),
                         "compile_matter_record")

    def run(text=RAW):
        source = temp_base_dir / "pipeline" / "inbox" / "agreement.txt"
        source.write_text(text)
        graph = bg.build_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "contract-test"}}
        states, order = {}, []
        pending = None
        for mode, event in graph.stream({
            "file_path": str(source), "matter_id": "CONTRACT-TEST", "messages": [],
        }, config, stream_mode=["updates", "values"]):
            if mode == "updates":
                for node in event:
                    order.append(node)
                    pending = node
            elif pending:
                states[pending] = deepcopy(event)
        final = graph.get_state(config)
        return order, states, final, source

    run.sorter, run.extractor, run.reporter = sorter, extractor, reporter
    run.base = temp_base_dir
    return run


def test_intake_to_archive_preserves_node_outputs_and_original_bytes(run_graph):
    order, states, final, source = run_graph()
    assert order == ["intake", "classify", "extract", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]
    intake, classified, extracted = (states[n] for n in ("intake", "classify", "extract"))
    assert intake["doc_text"] == CLEAN
    assert intake["intake_changed"] is True
    assert intake["intake_handoff"]["reason"] == "flag_off"
    assert run_graph.sorter.call_args.args == (CLEAN,)
    assert run_graph.sorter.call_args.kwargs["pages"] == intake["doc_pages"] == []
    assert classified["doc_type"] == "contract"
    assert classified["contract_subtype"] == "other"
    assert classified["classification_attempts"] == 1
    text, pages, handoff = run_graph.extractor.call_args.args
    assert (text, pages) == (CLEAN, [])
    assert "other" in handoff
    assert extracted["extracted_data"]["parties"] == FIELDS["parties"]
    assert extracted["extraction_confidence"] == 0.99
    report_input = run_graph.reporter.call_args.args[0]
    assert report_input["extracted_data"] == extracted["extracted_data"]
    assert report_input["classification_confidence"] == classified["classification_confidence"]
    report = states["compile_report"]["extracted_data"]["_report"]
    assert report["extracted_data"] == {k: extracted["extracted_data"][k] for k in report["extracted_data"]}
    assert report["extracted_data"]["contract_value"] == "$1,250.00"
    assert final.values["stage"] == "archived"
    doc_id = final.values["doc_id"]
    manifest = json.loads((run_graph.base / "manifests" / f"{doc_id}.json").read_text())
    assert manifest["extracted_data"] == states["compile_report"]["extracted_data"]
    assert manifest["doc_type"] == classified["doc_type"]
    with sqlite3.connect(run_graph.base / "mailroom.db") as db:
        stage, payload = db.execute("SELECT stage, extracted_data FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    assert stage == "archived"
    assert json.loads(payload) == manifest["extracted_data"]
    archived = list((run_graph.base / "archive").rglob("*.txt"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == RAW.encode()
    assert hashlib.sha256(archived[0].read_bytes()).hexdigest() == intake["file_sha256"]
    from storage.audit_log import get_audit_chain
    from schemas.audit import AuditLogEntry, verify_chain

    audit = asyncio.run(get_audit_chain(doc_id))
    # Unified audit row (M5): stage events are trace visits; only the terminal write is a row.
    assert [entry["event"] for entry in audit] == ["archived"]
    assert audit[-1]["detail"]["file_sha256"] == intake["file_sha256"]
    ingested = final.values["audit_trace"][0]["decision"]["audit_event"]
    assert ingested["event"] == "ingested" and ingested["detail"]["file_sha256"] == intake["file_sha256"]
    assert verify_chain([AuditLogEntry(doc_id=doc_id, **{k: v for k, v in entry.items() if k != "seq"}) for entry in audit])
    assert not source.exists()


def test_transient_classification_retries_keep_clean_text_and_attempt_budget(run_graph):
    success = deepcopy(run_graph.sorter.return_value)
    run_graph.sorter.side_effect = [openai.APIConnectionError(request=None), success]
    order, states, final, _ = run_graph()
    assert order == ["intake", "classify", "classify", "extract", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]
    assert [c.args[0] for c in run_graph.sorter.call_args_list] == [CLEAN, CLEAN]
    assert states["classify"]["classification_attempts"] == 1
    assert states["classify"]["transient_error"] is False
    assert final.values["stage"] == "archived"


def test_guarded_extraction_is_replaced_before_report_and_archive(run_graph):
    run_graph.extractor.side_effect = [
        {"parties": "invalid list shape", "confidence": 0.99},
        {**deepcopy(FIELDS), "confidence": 0.99},
    ]
    order, states, final, _ = run_graph()
    assert order == ["intake", "classify", "extract", "retry_extract", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]
    assert states["extract"]["extraction_guardrail"]
    assert states["retry_extract"]["extraction_attempts"] == 2
    assert states["retry_extract"]["extracted_data"]["parties"] == ["Acme", "Beta"]
    assert "invalid list shape" in run_graph.extractor.call_args_list[1].args[2]
    assert [c.args[0] for c in run_graph.extractor.call_args_list] == [CLEAN, CLEAN]
    assert run_graph.reporter.call_args.args[0]["extracted_data"] == states["retry_extract"]["extracted_data"]
    assert final.values["extracted_data"]["_report"]["extracted_data"]["parties"] == ["Acme", "Beta"]


@pytest.mark.parametrize("text", [" \t\r\n", "# \n\n---\n\n```\n```"])
def test_unreadable_intake_parks_before_any_model_or_extraction(run_graph, text):
    order, states, final, _ = run_graph(text)
    assert order == ["intake", "classify", "__interrupt__"]
    run_graph.sorter.assert_not_called()
    run_graph.extractor.assert_not_called()
    run_graph.reporter.assert_not_called()
    assert final.next == ("human_review",)
    assert states["classify"]["escalation_reason"] == "Empty or unreadable document content"
    review_files = list((run_graph.base / "pipeline" / "review").glob("*.txt"))
    assert len(review_files) == 1
    manifest = json.loads((run_graph.base / "manifests" / f"{final.values['doc_id']}.json").read_text())
    assert manifest["stage"] == "review"
    assert not list((run_graph.base / "archive").rglob("*.txt"))


def test_exhausted_invalid_extraction_cannot_reach_report_or_archive(run_graph):
    run_graph.extractor.side_effect = lambda *a: {"parties": "invalid list shape", "confidence": 0.99}
    order, states, final, _ = run_graph()
    assert order == ["intake", "classify", "extract", "retry_extract", "retry_extract", "__interrupt__"]
    assert states["retry_extract"]["extraction_attempts"] == 3
    assert states["retry_extract"]["extraction_guardrail"]
    assert final.next == ("human_review",)
    run_graph.reporter.assert_not_called()
    assert not list((run_graph.base / "archive").rglob("*.txt"))
    manifest = json.loads((run_graph.base / "manifests" / f"{final.values['doc_id']}.json").read_text())
    assert manifest["stage"] == "review"
    assert manifest["extracted_data"]["parties"] == "invalid list shape"


@pytest.mark.parametrize("llm_fails", [False, True])
def test_optional_intake_pass_keeps_prepared_text_and_advisory_prior(run_graph, monkeypatch, mocker, llm_fails):
    monkeypatch.setenv("MAILROOM_LLM_INTAKE", "1")
    mocker.patch("agents.intake.should_llm_intake", return_value=True)
    # Script the external model response, retaining real intake validation and
    # normalization of any proposed repair before it reaches classification.
    mocker.patch("agents.intake.IntakeAgent.__init__", return_value=None)
    call = mocker.patch("agents.intake.IntakeAgent._call_structured")
    if llm_fails:
        call.side_effect = RuntimeError("intake provider unavailable")
    else:
        call.return_value = {
            "triage": {"primary_doc_class": "contract", "confidence": 0.96,
                       "gist": "Service agreement between Acme and Beta", "keywords": ["Acme"]},
            "cleaned_text": "# Service Agreement\n\nAcme and **Beta** agreement.\n\nGoverning law: `New York`.",
            "changes_applied": ["Removed OCR artifacts"], "sections": [],
        }
    order, states, final, _ = run_graph()
    assert order == ["intake", "classify", "extract", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]
    assert states["intake"]["doc_text"] == CLEAN
    assert run_graph.sorter.call_args.args == (CLEAN,)
    prior = run_graph.sorter.call_args.kwargs["intake_prior"]
    if llm_fails:
        assert states["intake"]["intake_prep"] is None
        assert prior == ""
    else:
        assert "advisory" in prior
        assert "primary class: contract" in prior
        assert "Service agreement between Acme and Beta" in prior
    assert final.values["stage"] == "archived"


def test_reviewer_winning_class_controls_specialist_and_report(run_graph, mocker):
    run_graph.sorter.return_value = {
        "doc_type": "correspondence", "doc_subclass": "memo", "confidence": 0.90,
        "reasoning": "Uncertain memo",
    }
    mocker.patch("agents.sorter_reviewer.SorterReviewerAgent.__init__", return_value=None)
    reviewer = mocker.patch("agents.sorter_reviewer.SorterReviewerAgent.review", return_value={
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99,
        "reasoning": "Agreement between two parties",
    })
    order, states, final, _ = run_graph()
    assert order == ["intake", "classify", "retry_classify", "review_classify", "extract", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]
    assert reviewer.call_args.args == (CLEAN,)
    assert states["review_classify"]["review_verdict"] == "reviewer_overrides"
    assert states["review_classify"]["doc_type"] == "contract"
    run_graph.extractor.assert_called_once()
    assert "contract" in run_graph.extractor.call_args.args[2]
    assert run_graph.reporter.call_args.args[0]["doc_type"] == "contract"
    assert final.values["extracted_data"]["_report"]["doc_type"] == "contract"


def test_judge_findings_and_arbiter_fix_list_reach_reextraction(run_graph, mocker):
    first = {**deepcopy(FIELDS), "governing_law": "New Jersey"}
    run_graph.extractor.side_effect = [
        {**first, "confidence": 0.95}, {**deepcopy(FIELDS), "confidence": 0.99},
    ]
    mocker.patch("agents.judge.CompletenessJudge.__init__", return_value=None)
    judge = mocker.patch("agents.judge.CompletenessJudge.judge_completeness", return_value={
        "completeness_label": "partial", "completeness": 0.6,
        "reasoning": "governing_law contradicts source",
    })
    mocker.patch("agents.arbiter.ArbiterAgent.__init__", return_value=None)
    arbiter = mocker.patch("agents.arbiter.ArbiterAgent.arbitrate", return_value={
        "decision": "retry_extraction", "reasoning": "Correct the governing law",
        "handoff_summary": "Read the governing law clause again",
        "fields_to_fix": ["governing_law"],
    })
    order, states, final, _ = run_graph()
    assert order == ["intake", "classify", "extract", "judge_verify", "arbiter", "retry_extract", "compile_report", "score_common", "score_contract", "catalog_write", "archive"]
    assert judge.call_args.kwargs == {"doc_type": "contract", "extracted": {**first, "cuad_family": None, "cuad_clauses": [], "maud_clauses": [], "merger_consideration": None}, "doc_text": CLEAN}
    assert arbiter.call_args.kwargs["extracted"] == {**first, "cuad_family": None, "cuad_clauses": [], "maud_clauses": [], "merger_consideration": None}
    assert arbiter.call_args.kwargs["judge_findings"] == ["governing_law contradicts source"]
    assert arbiter.call_args.kwargs["judge_verdict"] == "partial"
    assert arbiter.call_args.kwargs["doc_text"] == CLEAN
    retry_context = run_graph.extractor.call_args_list[1].args[2]
    assert "governing_law" in retry_context
    assert "Read the governing law clause again" in retry_context
    assert states["retry_extract"]["arbiter_retry_count"] == 1
    assert run_graph.reporter.call_args.args[0]["extracted_data"]["governing_law"] == "New York"
    assert final.values["extracted_data"]["_report"]["extracted_data"]["governing_law"] == "New York"
