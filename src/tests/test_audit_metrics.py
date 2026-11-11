"""Roadmap M11: ``audit_row_metrics`` (spec §7)."""

from __future__ import annotations


def _scoring(overall=0.5, headline=None, f1=0.6, f2=0.7, status="scored", payload="model"):
    return {"overall_score": overall, "headline_score": headline, "extraction_f1": f1, "extraction_f2": f2,
            "status": status, "scored_payload": payload}


def _row(event="archived", **detail):
    base = {
        "pipeline_success": event == "archived", "doc_type": "contract", "doc_subclass": "nda",
        "review_decision": None, "classification_attempts": 1,
        "extraction": {"extraction_attempts": 1, "adherence": {"schema_adherence": 0.9}},
        "scoring": _scoring(), "metrics": {"success_rate": 1, "class_correct": 1, "subclass_correct": 0,
                                           "run_duration_seconds": 2.5},
        "trace": [{"node": "classify", "prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.01},
                  {"node": "extract", "prompt_tokens": 30, "completion_tokens": 5, "cost_usd": 0.02},
                  {"node": "archive"}],
    }
    base.update(detail)
    return {"entry_id": "row-1", "doc_id": "d", "event": event, "detail": base}


def _att(stage, n, trigger="initial", ended="2026-11-03T09:00:0{}+00:00", **detail):
    return {"attempt_id": f"{stage}-{n}", "stage": stage, "attempt_no": n, "trigger": trigger,
            "ended_at": ended.format(n), "detail": detail}


def test_metrics_keys_exactly_spec():
    from audit.metrics import METRIC_KEYS, audit_row_metrics

    assert METRIC_KEYS == (
        "outcome", "pipeline_success", "first_pass", "doc_type", "doc_subclass", "class_correct",
        "subclass_correct", "overall_extraction_score", "suite_overall_extraction_score", "extraction_f1",
        "extraction_f2", "schema_adherence", "scoring_status",
        "model_overall_extraction_score", "model_suite_overall_extraction_score", "model_extraction_f1",
        "model_extraction_f2", "model_schema_adherence", "model_scoring_status",
        "classification_attempts", "extraction_attempts", "arbiter_rescued", "prompt_tokens",
        "completion_tokens", "cost_usd", "latency_ms", "reviewed", "human_corrected", "rescored", "resumed",
        "recovered",
    )
    out = audit_row_metrics(_row(), [], [])
    assert tuple(out) == METRIC_KEYS
    assert out["outcome"] == "archived" and out["pipeline_success"] is True and out["first_pass"] is True
    assert (out["class_correct"], out["subclass_correct"]) == (True, False)
    assert (out["prompt_tokens"], out["completion_tokens"]) == (40, 7)
    assert abs(out["cost_usd"] - 0.03) < 1e-9 and out["latency_ms"] == 2500.0
    assert out["schema_adherence"] == 0.9 and out["scoring_status"] == "scored"
    assert (out["reviewed"], out["human_corrected"], out["rescored"], out["resumed"], out["recovered"]) == (
        False, False, False, False, False)
    # With no sub-rows the row's numbers are the model's.
    assert out["model_extraction_f1"] == 0.6


def test_headline_mapping():
    from audit.metrics import audit_row_metrics

    plain = audit_row_metrics(_row(), [], [])
    assert plain["overall_extraction_score"] == plain["suite_overall_extraction_score"] == 0.5
    head = audit_row_metrics(_row(scoring=_scoring(overall=0.5, headline=0.8)), [], [])
    assert head["overall_extraction_score"] == 0.8 and head["suite_overall_extraction_score"] == 0.5


def test_latest_rescore_supplies_scores():
    from audit.metrics import audit_row_metrics

    adds = [
        {"kind": "rescore", "seq": 1, "detail": {"scoring": _scoring(overall=0.1)}},
        {"kind": "rescore", "seq": 2, "detail": {"scoring": _scoring(overall=0.9, f1=0.95),
                                                  "extraction_adherence": {"schema_adherence": 1.0}}},
    ]
    out = audit_row_metrics(_row(), adds, [])
    assert out["suite_overall_extraction_score"] == 0.9 and out["extraction_f1"] == 0.95
    assert out["schema_adherence"] == 1.0 and out["rescored"] is True


def test_resumed_supplies_outcome():
    from audit.metrics import audit_row_metrics

    adds = [{"kind": "resumed", "seq": 1, "detail": {"event": "archived", "pipeline_success": True}}]
    out = audit_row_metrics(_row(event="triage_reviewed", pipeline_success=False), adds, [])
    assert out["outcome"] == "archived" and out["pipeline_success"] is True and out["resumed"] is True
    review = [{"kind": "review_decision", "seq": 1, "detail": {}}]
    assert audit_row_metrics(_row(), review, [])["reviewed"] is True


def test_model_only_counterparts_from_last_model_attempt():
    from audit.metrics import audit_row_metrics

    attempts = [
        _att("classification", 1),
        _att("classification", 2, trigger="arbiter_rescue"),
        _att("extraction", 1, scoring=_scoring(overall=0.2, f1=0.25), adherence={"schema_adherence": 0.5}),
        _att("extraction", 2, trigger="retry", scoring=_scoring(overall=0.4, f1=0.45),
             adherence={"schema_adherence": 0.75}),
        _att("extraction", 3, trigger="human_correction", scoring=_scoring(overall=1.0, payload="human_corrected")),
    ]
    row = _row(scoring=_scoring(overall=1.0, f1=1.0, payload="human_corrected"))
    out = audit_row_metrics(row, [], attempts)
    assert out["human_corrected"] is True and out["arbiter_rescued"] is True
    assert out["suite_overall_extraction_score"] == 1.0  # the row scores the corrected payload
    assert out["model_suite_overall_extraction_score"] == 0.4 and out["model_extraction_f1"] == 0.45
    assert out["model_schema_adherence"] == 0.75
    assert (out["classification_attempts"], out["extraction_attempts"]) == (2, 2)


def test_corrected_without_model_attempt_leaves_model_scores_null():
    from audit.metrics import audit_row_metrics

    attempts = [_att("extraction", 1, trigger="human_correction")]
    out = audit_row_metrics(_row(), [], attempts)
    assert out["human_corrected"] is True
    assert out["model_suite_overall_extraction_score"] is None and out["model_scoring_status"] is None


def test_recovered_flag_and_missing_fields_are_null():
    from audit.metrics import audit_row_metrics

    out = audit_row_metrics({"event": "failed", "detail": {"recovered_from": {"sink": "sql"}}}, [], [])
    assert out["recovered"] is True and out["outcome"] == "failed"
    for key in ("first_pass", "class_correct", "overall_extraction_score", "prompt_tokens", "latency_ms",
                "classification_attempts"):
        assert out[key] is None, key
