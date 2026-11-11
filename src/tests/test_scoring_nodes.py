"""Roadmap M6: the scoring node family (spec §3.1, §5.1)."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import MemorySaver

from graph import build_graph as bg
from graph import scoring_nodes as sn

TYPES = ("contract", "corporate_record", "correspondence", "insurance_claim", "merger_agreement")


def test_one_node_per_registry_entry():
    from llm_dojo_scoring.scoring.doc_scorers import DOC_SCORERS

    nodes = {n for n in bg.build_graph().get_graph().nodes if n.startswith("score_")}
    assert nodes == {"score_common", "score_generic", *(f"score_{t}" for t in DOC_SCORERS)}
    assert sn.scorer_types() == (*sorted(DOC_SCORERS), "generic")
    edges = {(e.source, e.target) for e in bg.build_graph().get_graph().edges}
    assert ("compile_report", "score_common") in edges
    assert ("compile_report", "catalog_write") not in edges
    for name in nodes - {"score_common"}:
        assert ("score_common", name) in edges and (name, "catalog_write") in edges


@pytest.mark.parametrize("doc_type", TYPES)
def test_routes_each_corpus_type(doc_type):
    assert sn.score_route({"doc_type": doc_type}) == f"score_{doc_type}"


@pytest.mark.parametrize("doc_type", [None, "", "due_diligence", "court_opinion", "no_such_type"])
def test_corpus_absent_and_unknown_route_to_generic(doc_type):
    assert sn.score_route({"doc_type": doc_type}) == "score_generic"


def test_routes_on_final_doc_type_after_override():
    # The sorter said contract; a person corrected it to correspondence on the tray.
    state = {"doc_type": "correspondence", "reviewer_doc_type": "contract", "classification_attempts": 1}
    assert sn.score_route(state) == "score_correspondence"
    out = sn.score_common_node(state)
    assert out["scoring_common"]["scorer"] == "correspondence"
    assert out["scoring_common"]["doc_type"] == "correspondence"


def test_no_ground_truth_is_unscorable():
    out = sn.score_document_block({"doc_type": "contract", "extracted_data": {"parties": ["Acme"]}})
    assert (out["scoring"]["status"], out["scoring"]["reason"]) == ("unscorable", "gt_missing")
    assert out["scoring"]["scored_payload"] == "model"
    assert out["extraction_adherence"]["parse_error"] is False


@pytest.mark.parametrize("payload", ["The parties are Acme and Beta.", None])
def test_non_dict_extraction_still_scored(payload):
    state = {"doc_type": "contract", "extracted_data": payload,
             "ground_truth": {"expected_fields": {"parties": ["Acme", "Beta"]}}}
    out = sn.score_document_block(state)
    assert out["extraction_adherence"]["parse_error"] is True
    assert out["scoring"]["status"] == "scored" and out["scoring"]["overall_score"] == 0.0
    # The node wraps the same function and never raises.
    common = sn.score_common_node(state)
    assert common["extraction_adherence"]["parse_error"] is True


def test_field_map_sha_from_mailroom_taxonomy():
    out = sn.score_document_block({"doc_type": "contract", "extracted_data": {}})
    blob = sn.TAXONOMY_PATH.read_bytes()
    # The dojo records the git blob id of the taxonomy that was passed in.
    expected = hashlib.sha1(f"blob {len(blob)}\0".encode() + blob).hexdigest()
    assert out["scoring"]["field_map_sha"] == expected


def test_pipeline_keys_are_not_scored():
    payload = {"parties": ["Acme"], "_report": {"summary": "x"}}
    assert sn.model_payload(payload) == {"parties": ["Acme"]}
    out = sn.score_document_block({"doc_type": "contract", "extracted_data": payload})
    assert out["extraction_adherence"]["extra_schema_keys"] == []


def test_human_corrected_payload_is_marked():
    out = sn.score_document_block({"doc_type": "contract", "extracted_data": {}, "extraction_source": "human"})
    assert out["scoring"]["scored_payload"] == "human_corrected"


def test_scorer_failure_is_recorded_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scorer broke")

    monkeypatch.setattr("llm_dojo_scoring.score_archive_block", boom)
    out = sn._make_type_node("contract")({"doc_type": "contract", "extracted_data": {}})
    assert out["scoring"]["status"] == "error" and "scorer broke" in out["scoring"]["reason"]
    assert out["scoring"]["scorer"]["doc_type"] == "contract"


def test_benchmark_rows_get_headline():
    cuad = sn.score_document_block({
        "doc_type": "contract", "extracted_data": {"parties": ["Acme"]},
        # The raw map stays at the top level of the run's ground truth (G3); expected_fields has the flat list.
        "ground_truth": {"expected_fields": {"parties": ["Acme"], "cuad_clauses": ["Parties: Acme"]},
                         "cuad_clause_labels": {"Parties": ["Acme"], "Governing Law": ["New York"]}},
    })
    assert cuad["scoring"]["headline_metric"] == "cuad_presence_f1"
    assert cuad["scoring"]["headline_score"] is not None
    maud = sn.score_document_block({
        "doc_type": "merger_agreement", "extracted_data": {"acquirer": "Acme"},
        "ground_truth": {"expected_fields": {"acquirer": "Acme"},
                         "maud_clause_labels": {"Type of Consideration": {"answer": "All Cash"}}},
    })
    assert maud["scoring"]["headline_metric"] == "maud_accuracy"
    assert maud["scoring"]["headline_score"] == 0.0


def test_scoring_ground_truth_passes_raw_maps_through():
    from observability.extraction_gt import scoring_ground_truth

    raw = {"Parties": ["Acme"]}
    assert scoring_ground_truth({"cuad_clause_labels": raw}) == {"cuad_clause_labels": raw}
    inner = {"expected_fields": {"cuad_clause_labels": {"x": 1}}, "cuad_clause_labels": raw}
    assert scoring_ground_truth(inner)["cuad_clause_labels"] == {"x": 1}
    assert scoring_ground_truth({}) is None and scoring_ground_truth(None) is None


def test_doc_subclass_from_final_state(mocker):
    import llm_dojo_scoring

    spy = mocker.spy(llm_dojo_scoring, "score_archive_block")
    sn.score_document_block({"doc_type": "correspondence", "doc_subclass": "memo", "extracted_data": {},
                             "ground_truth": {"expected_fields": {"sender": "A"}, "expected_subclass": "letter"}})
    kwargs = spy.call_args.kwargs
    assert kwargs["doc_subclass"] == "memo" and kwargs["expected_subclass"] == "letter"


@pytest.fixture
def run_graph(temp_base_dir, monkeypatch, mocker):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAILROOM_MEMORY_DIR", str(temp_base_dir / "memory"))
    mocker.patch("agents.sorter.SorterAgent.classify_json", return_value={
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "Agreement",
    })
    fields = {"document_name": "Service Agreement", "parties": ["Acme", "Beta"], "governing_law": "New York"}
    mocker.patch.object(bg, "_extract_contracts", side_effect=lambda *a: {**deepcopy(fields), "confidence": 0.99})

    def run(ground_truth=None):
        source = temp_base_dir / "pipeline" / "inbox" / "agreement.txt"
        source.write_text("Service Agreement between Acme and Beta. Governing law: New York.")
        graph = bg.build_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "score-test"}}
        start = {"file_path": str(source), "matter_id": "SCORE", "messages": []}
        if ground_truth:
            start["ground_truth"] = ground_truth
        order = [n for ev in graph.stream(start, config, stream_mode="updates") for n in ev]
        return order, graph.get_state(config).values

    run.fields = fields
    return run


def test_each_scoring_node_has_trace_visit(run_graph):
    order, final = run_graph({"expected_doc_class": "contract", "expected_fields": dict(run_graph.fields)})
    assert order[-4:] == ["score_common", "score_contract", "catalog_write", "archive"]
    visits = {v["node"]: v for v in final["audit_trace"]}
    assert visits["score_common"]["decision"]["scoring_common"]["scorer"] == "contract"
    scoring = visits["score_contract"]["decision"]["scoring"]
    assert scoring["status"] == "scored" and scoring["overall_score"] == 1.0
    assert final["scoring"]["scorer"]["doc_type"] == "contract"
    assert final["extraction_adherence"]["parse_error"] is False
