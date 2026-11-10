"""Roadmap M7: ``build_row`` and ``attempts_from_trace`` (spec §4.1, §4.2)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from tests.test_audit_trace import run_graph  # noqa: F401  (fixture)

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def _visit(i, node, attempt_id=None, outcome="ok", **decision):
    return {
        "visit_id": f"v{i}", "node": node, "attempt_id": attempt_id, "status": "ok", "outcome": outcome,
        "started_at": (T0 + timedelta(seconds=i)).isoformat(),
        "ended_at": (T0 + timedelta(seconds=i, milliseconds=500)).isoformat(),
        "model": "m" if attempt_id else None, "prompt_tokens": 10 if attempt_id else None,
        "completion_tokens": 2 if attempt_id else None, "cost_usd": None,
        "decision": decision, "decisions": [],
    }


def test_detail_keys_exactly_spec():
    from audit.row import ROW_DETAIL_KEYS, ROW_SCHEMA, build_row

    # The §4.1 table, in order.
    assert ROW_DETAIL_KEYS == (
        "row_schema", "stage", "pipeline_success", "failure_class", "error_message", "escalation_reason",
        "run_attempt", "review_decision", "arbiter_decision", "classification_arbiter_decision", "doc_type",
        "doc_subclass", "classification_confidence", "classification_attempts", "extraction", "judge_verdict",
        "judge_score", "judge_findings", "scoring", "metrics", "nodes_visited", "trace", "trace_mode",
        "trace_id", "trace_incomplete", "attempts", "refs",
    )
    row = build_row({"doc_id": "d", "matter_id": "M", "doc_type": "contract"}, event="archived", actor="archivist")
    assert tuple(row.detail) == ROW_DETAIL_KEYS
    assert row.detail["row_schema"] == ROW_SCHEMA == 1
    assert (row.doc_id, row.matter_id, row.event, row.actor) == ("d", "M", "archived", "archivist")


def test_uncollected_fields_are_null():
    from audit.row import EXTRACTION_KEYS, REF_KEYS, build_row

    detail = build_row({"doc_id": "d"}, event="failed", actor="pipeline").detail
    for key in ("failure_class", "error_message", "escalation_reason", "run_attempt", "review_decision",
                "arbiter_decision", "classification_arbiter_decision", "doc_type", "doc_subclass",
                "classification_confidence", "classification_attempts", "judge_verdict", "judge_score",
                "judge_findings", "scoring", "metrics", "trace_id"):
        assert detail[key] is None, key
    assert tuple(detail["extraction"]) == EXTRACTION_KEYS
    assert all(v is None for v in detail["extraction"].values())
    assert detail["refs"] == {k: None for k in REF_KEYS}
    assert detail["stage"] == "failed" and detail["pipeline_success"] is False
    assert detail["nodes_visited"] == [] and detail["trace"] == [] and detail["trace_incomplete"] is False


def test_metrics_equal_emit_pipeline_scores(run_graph, temp_base_dir, mocker):
    from audit.writer import get_row
    from graph import build_graph as bg
    from observability import scores

    emitted = mocker.spy(scores, "emit_pipeline_scores")
    source = temp_base_dir / "pipeline" / "inbox" / "metrics.txt"
    source.write_text("Service Agreement between Acme and Beta. Governing law: New York.")
    result = bg.run_pipeline(source, "M-METRICS")
    assert result["stage"] == "archived"
    row = asyncio.run(get_row(result["doc_id"]))
    metrics = dict(row["detail"]["metrics"])
    metrics.pop("subclass_correct", None)
    assert emitted.spy_return == metrics
    # The emitter was handed the row's numbers instead of computing its own.
    assert emitted.call_args.kwargs["precomputed"] == metrics


def test_subrows_per_retry_and_arbiter_rescue():
    from audit.row import attempts_from_trace

    trace = [
        _visit(0, "intake"),
        _visit(1, "classify", "att-d-classification-r0-1", outcome="retry"),  # transient self-loop
        _visit(2, "classify", "att-d-classification-r0-1", doc_type="contract", classification_confidence=0.5),
        _visit(3, "retry_classify", "att-d-classification-r0-2", doc_type="contract",
               classification_confidence=0.95),
        _visit(4, "extract", "att-d-extraction-r0-1", extracted_data={"parties": []}),
        _visit(5, "judge_verify", "att-d-judge-r0-1", judge_verdict="incomplete"),
        _visit(6, "arbiter", "att-d-judge-r0-1", arbiter_decision="retry_extract"),
        _visit(7, "retry_extract", "att-d-extraction-r0-2", extracted_data={"parties": ["Acme"]}),
        _visit(8, "compile_report"),
    ]
    subs = attempts_from_trace(trace, {"doc_type": "contract"})
    assert [(s.stage, s.attempt_no, s.trigger) for s in subs] == [
        ("classification", 1, "initial"),
        ("classification", 2, "retry"),
        ("extraction", 1, "initial"),
        ("judge", 1, "initial"),
        ("extraction", 2, "arbiter_rescue"),
    ]
    first = subs[0]
    assert first.detail["nodes"] == ["classify", "classify"] and first.detail["prompt_tokens"] == 20
    assert first.detail["confidence"] == 0.5 and subs[1].detail["confidence"] == 0.95
    assert subs[3].detail["nodes"] == ["judge_verify", "arbiter"]
    assert subs[3].detail["arbiter_decision"] == "retry_extract"
    assert subs[4].detail["extracted_data"] == {"parties": ["Acme"]}
    assert subs[4].detail["adherence"] is not None
    assert len({s.attempt_id for s in subs}) == len(subs)
    # The same attempt id met again after a human review is its own sub-row.
    resumed = attempts_from_trace(trace[:5] + [_visit(9, "human_review"),
                                               _visit(10, "extract", "att-d-extraction-r0-1")])
    assert [s.attempt_id for s in resumed][-2:] == ["att-d-extraction-r0-1", "att-d-extraction-r0-1.2"]
    assert resumed[-1].trigger == "resume"


def test_trace_incomplete_flag_on_old_checkpoint():
    from audit.row import build_row

    trace = [_visit(1, "classify", "att-d-classification-r0-1", doc_type="contract")]
    row = build_row({"doc_id": "d", "audit_trace": trace, "trace_incomplete": True},
                    event="archived", actor="archivist", terminal_node="archive")
    assert row.detail["trace_incomplete"] is True
    # What the run kept is still recorded, with the terminal node appended.
    assert row.detail["nodes_visited"] == ["classify", "archive"]
    assert [s.stage for s in row.attempts] == ["classification"]


def _old_validate(doc_type, extracted):
    """``validate_extraction`` before G6: pydantic only."""
    from schemas.documents import get_extraction_schema

    if not doc_type or not extracted or extracted.get("_parse_error"):
        return False
    model = get_extraction_schema(doc_type)
    if model is None:
        return False
    try:
        model.model_validate(extracted)
        return True
    except Exception:
        return False


PAYLOADS = [
    {"parties": ["Acme", "Beta"], "governing_law": "New York", "confidence": 0.9},
    {"parties": "Acme"},  # wrong type
    {"claimant": "Acme", "claim_number": "C-1", "reasoning": "x"},
    {"sender": "A", "recipients": ["B"], "_raw": "{...}"},
    {"acquirer": "Acme", "target": "Beta", "deal_value": "not a number"},
    {"entity_name": "Acme Inc", "officers": [1, 2]},
    {"unknown_key": 1},
    {"_parse_error": True, "_raw": "garbage"},
    {"error": "upstream"},
]


def test_validate_extraction_delegates_to_dojo_with_same_verdicts(mocker):
    """G6: ``schema_valid`` comes from ``assess_extraction_payload`` and no routing verdict moves."""
    import llm_dojo_scoring

    from observability.scores import validate_extraction
    from schemas.documents import EXTRACTION_SCHEMAS

    spy = mocker.spy(llm_dojo_scoring, "assess_extraction_payload")
    for doc_type in (*EXTRACTION_SCHEMAS, "unknown", "court_opinion", "", None):
        for payload in (*PAYLOADS, {}, None):
            got = validate_extraction(doc_type, payload)
            assert got["schema_valid"] is _old_validate(doc_type, payload), (doc_type, payload)
    assert spy.call_count > 0
