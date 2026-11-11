"""Roadmap M8: human review paths on the unified audit row (spec §4.3, §5.4)."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_hitl_resolution import (  # noqa: F401  (fixtures)
    _extraction_park, _forbid_llm, _resolve, _resp, _sorter_park, api, llm,
)

# The M8 review paths write to the unified row, so the database must be
# migrated first (an explicit operator step, never automatic).
pytestmark = pytest.mark.usefixtures("migrated_audit_db")

# One reply for both judge calls (completeness, then factual accuracy).
JUDGE_PASS = ('{"completeness_label": "complete", "completeness": 0.95, "extraction_correctness_label": '
              '"accurate", "extraction_correctness": 0.95, "reasoning": "ok"}')


def _row(doc_id):
    from audit.writer import get_row

    return asyncio.run(get_row(doc_id))


def _after_review(row):
    nodes = row["detail"]["nodes_visited"]
    return nodes[len(nodes) - nodes[::-1].index("human_review"):]


def _judge_passes(llm):
    llm.chat.completions.create.side_effect = None
    llm.chat.completions.create.return_value = _resp(JUDGE_PASS)


def test_complete_without_data_scores_and_archives_model_extraction(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "as-is.txt")
    _forbid_llm(mocker, llm)
    assert _resolve(api, doc_id, disposition="complete", decision="approved").status_code == 200  # legacy, no data
    row = _row(doc_id)
    detail = row["detail"]
    assert row["event"] == "archived"
    assert detail["pipeline_success"] is False
    assert detail["extraction"]["extraction_source"] == "model"
    assert detail["scoring"]["scored_payload"] == "model"
    after = _after_review(row)
    assert after[0] == "compile_report" and "score_common" in after and after[-1] == "archive"
    assert "extract" not in after
    assert not [s for s in row["sub_rows"] if s["trigger"] == "human_correction"]


def test_complete_with_data_is_human_correction(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "corrected.txt")
    _forbid_llm(mocker, llm)
    fixed = {"parties": ["Acme Corp", "Beta LLC"]}
    assert _resolve(api, doc_id, resolution="correct", field_corrections=fixed, reviewer="pat").status_code == 200
    row = _row(doc_id)
    detail = row["detail"]
    assert detail["extraction"]["extraction_source"] == "human"
    assert detail["scoring"]["scored_payload"] == "human_corrected"
    assert "extract" not in _after_review(row) and "retry_extract" not in _after_review(row)
    (corr,) = [s for s in row["sub_rows"] if s["trigger"] == "human_correction"]
    assert corr["stage"] == "extraction" and corr["detail"]["reviewer"] == "pat"
    assert corr["detail"]["resolved_at"] and corr["started_at"]
    assert corr["detail"]["diff"]["changed"] == {"parties": {"before": ["Acme Corp"], "after": fixed["parties"]}}
    assert corr["detail"]["extracted_data"]["parties"] == fixed["parties"]


def test_model_scores_not_credited_with_correction(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "credit.txt")
    _forbid_llm(mocker, llm)
    assert _resolve(api, doc_id, resolution="correct",
                    field_corrections={"parties": ["Acme Corp", "Beta LLC"]}).status_code == 200
    row = _row(doc_id)
    model = [s for s in row["sub_rows"] if s["stage"] == "extraction" and s["trigger"] != "human_correction"]
    assert model, "the model's extraction attempts keep their own sub-rows"
    last = max(model, key=lambda s: s["attempt_no"])
    # The last model attempt is scored on the model's fields, not the person's.
    assert last["detail"]["extracted_data"]["parties"] == ["Acme Corp"]
    assert last["detail"]["adherence"] is not None
    (corr,) = [s for s in row["sub_rows"] if s["trigger"] == "human_correction"]
    assert corr["attempt_no"] == last["attempt_no"] + 1


def test_resume_reruns_extract(api, temp_base_dir, llm):
    doc_id = _sorter_park(temp_base_dir, "resume.txt")
    _judge_passes(llm)
    assert _resolve(api, doc_id, resolution="approve_as_is").status_code == 200
    row = _row(doc_id)
    assert row["event"] == "archived"
    after = _after_review(row)
    assert after[0] == "extract" and after[-1] == "archive"
    extraction = [s for s in row["sub_rows"] if s["stage"] == "extraction"]
    assert [s["trigger"] for s in extraction] == ["resume"]
    assert row["detail"]["extraction"]["extraction_source"] == "model"


def test_doc_type_override_reruns_extraction_and_routes_scoring(api, temp_base_dir, llm):
    doc_id = _extraction_park(temp_base_dir, "override.txt")
    _judge_passes(llm)
    r = _resolve(api, doc_id, resolution="reclassify", doc_type="correspondence")
    assert r.status_code == 200, r.text
    row = _row(doc_id)
    after = _after_review(row)
    assert after[0] == "extract"
    assert "score_correspondence" in after and "score_contract" not in after
    assert row["detail"]["doc_type"] == "correspondence"
    assert row["detail"]["scoring"]["scorer"]["doc_type"] == "correspondence"


def test_record_and_requeue_before_row_land_in_trace(api, temp_base_dir, llm, mocker):
    doc_id = _extraction_park(temp_base_dir, "record.txt")
    assert _resolve(api, doc_id, disposition="record", decision="approved", notes="seen").status_code == 200
    r = _resolve(api, doc_id, disposition="requeue", decision="approved")
    assert r.status_code == 200, r.text
    assert _row(doc_id) is None  # still parked: no row, no addendum
    _forbid_llm(mocker, llm)
    assert _resolve(api, doc_id, resolution="approve_as_is").status_code == 200
    row = _row(doc_id)
    events = [(v["node"], (v["decision"].get("audit_event") or {}).get("detail", {}).get("disposition"))
              for v in row["detail"]["trace"] if (v["decision"].get("audit_event") or {}).get("event")
              == "review_decision"]
    assert events == [("human_review", "record"), ("human_review", "requeue")]
    assert row["addenda"] == []


def test_record_after_row_is_review_decision_addendum(api, temp_base_dir, llm, mocker):
    from audit.events import record_document_event

    doc_id = _extraction_park(temp_base_dir, "after.txt")
    _forbid_llm(mocker, llm)
    assert _resolve(api, doc_id, resolution="approve_as_is").status_code == 200
    assert _resolve(api, doc_id, disposition="record", decision="approved", notes="audit").status_code == 200
    (add,) = _row(doc_id)["addenda"]
    assert add["kind"] == "review_decision" and add["actor"] == "human_reviewer"
    assert add["detail"]["disposition"] == "record" and add["detail"]["notes"] == "audit"
    assert asyncio.run(record_document_event(doc_id, "relations_linked", {"edges": []}, actor="relations")) \
        == "addendum"
    assert [a["kind"] for a in _row(doc_id)["addenda"]] == ["review_decision", "relations_linked"]
    # No row and nothing parked: nothing to attach to, and no error.
    assert asyncio.run(record_document_event("no-such-doc", "review_decision", {})) is None
