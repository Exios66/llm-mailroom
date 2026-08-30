"""Objective reconsideration — aligned with The-Mailroom PR #14."""

from pathlib import Path

import pytest

from graph.routing import (
    after_classify,
    after_extraction,
    after_report,
    after_retry_classify,
    after_review_classify,
)
from pipeline.reconsideration import (
    CLASS_MISS,
    EXTRACTION_MISS,
    HOLLOW_EXTRACTION,
    REPORTING_INCOMPLETE,
    align_class,
    class_misses_ground_truth,
    collect_review_causes,
    coverage_below_floor,
    expected_field_coverage,
    extraction_is_hollow,
    format_causes,
    report_is_failed,
    routing_coverage_expected_fields,
    should_reconsider,
)


def test_align_class_keeps_merger_distinct_from_contract():
    assert align_class("merger_agreement") == "merger_agreement"
    assert align_class("contract") == "contract"
    assert align_class("correspondence") == "correspondence"


def test_class_miss_even_at_high_confidence_goes_to_lane_a():
    state = {
        "doc_type": "contract",
        "classification_confidence": 0.99,
        "classification_attempts": 1,
        "ground_truth": {"expected_hf_class": "correspondence"},
    }
    assert class_misses_ground_truth(state) is True
    assert after_classify(state) == "review_classify"
    assert after_retry_classify(state) == "review_classify"


def test_predicting_contract_for_maud_gt_is_a_class_miss():
    state = {
        "doc_type": "contract",
        "classification_confidence": 0.99,
        "classification_attempts": 1,
        "ground_truth": {"expected_hf_class": "merger_agreement"},
    }
    assert class_misses_ground_truth(state) is True
    assert after_classify(state) == "review_classify"


def test_matching_merger_agreement_extracts():
    state = {
        "doc_type": "merger_agreement",
        "classification_confidence": 0.99,
        "classification_attempts": 1,
        "ground_truth": {"expected_hf_class": "merger_agreement"},
    }
    assert class_misses_ground_truth(state) is False
    assert after_classify(state) == "extract"


def test_live_run_without_gt_still_extracts_at_high_confidence():
    state = {
        "doc_type": "contract",
        "classification_confidence": 0.99,
        "classification_attempts": 1,
    }
    assert class_misses_ground_truth(state) is False
    assert after_classify(state) == "extract"


def test_reviewer_still_wrong_vs_gt_goes_to_human():
    state = {
        "review_verdict": "reviewer_agrees_high",
        "reviewer_confidence": 0.99,
        "reviewer_doc_type": "contract",
        "doc_type": "contract",
        "ground_truth": {"expected_doc_class": "correspondence"},
    }
    assert after_review_classify(state) == "human_review"


def test_reviewer_override_to_gt_class_extracts():
    state = {
        "review_verdict": "reviewer_overrides",
        "reviewer_confidence": 0.98,
        "reviewer_doc_type": "correspondence",
        "doc_type": "correspondence",
        "ground_truth": {"expected_hf_class": "correspondence"},
    }
    assert after_review_classify(state) == "extract"


def test_hollow_extraction_retries_then_reviews():
    hollow = {
        "doc_type": "contract",
        "extraction_confidence": 0.99,
        "extraction_attempts": 1,
        "extracted_data": {"confidence": 0.99, "mock_extraction": True},
    }
    assert extraction_is_hollow(hollow["extracted_data"]) is True
    assert after_extraction(hollow) == "retry_extract"
    assert after_extraction({**hollow, "extraction_attempts": 2}) == "retry_extract"
    assert after_extraction({**hollow, "extraction_attempts": 3}) == "human_review"


def test_expected_field_coverage_below_floor_retries():
    state = {
        "doc_type": "contract",
        "extraction_confidence": 0.99,
        "extraction_attempts": 1,
        "extracted_data": {"parties": ["Acme"], "governing_law": ""},
        "ground_truth": {
            "expected_fields": {
                "parties": ["Acme"],
                "governing_law": "Delaware",
                "effective_date": "2024-01-01",
            }
        },
    }
    assert expected_field_coverage(
        state["extracted_data"], state["ground_truth"]["expected_fields"]
    ) < 0.70
    assert after_extraction(state) == "retry_extract"
    assert after_extraction({**state, "extraction_attempts": 2}) == "retry_extract"
    assert after_extraction({**state, "extraction_attempts": 3}) == "human_review"


def test_routing_coverage_excludes_eval_extras_and_posthoc():
    expected = {
        "sender": "Pat",
        "recipient": "Kim",
        "communication_type": "email",
        "content_topic": "legal",
        "sentiment_label": "neutral",
        "communication_date": "2024-01-01",
    }
    scoped = routing_coverage_expected_fields(expected, "correspondence")
    assert "content_topic" not in scoped
    assert "sentiment_label" not in scoped
    assert scoped["sender"] == "Pat"
    extracted = {
        "sender": "Pat",
        "recipient": "Kim",
        "communication_type": "email",
        "communication_date": "2024-01-01",
    }
    assert coverage_below_floor(extracted, expected, doc_type="correspondence") is False

    posthoc_expected = {
        "claim_number": "123",
        "insurer": "CMS Medicare",
        "claimed_amount": 100.0,
        "coverage_determination": "approved",
    }
    scoped_ins = routing_coverage_expected_fields(
        posthoc_expected,
        "insurance_claim",
        sources={"claimed_amount": "posthoc", "claim_number": "hub"},
    )
    assert scoped_ins is not None
    assert "claimed_amount" not in scoped_ins
    assert scoped_ins["claim_number"] == "123"


def test_correspondence_null_recipient_validates():
    from observability.scores import validate_extraction

    payload = {
        "sender": "Acme Corp",
        "recipient": None,
        "additional_recipients": [],
        "communication_type": "press_release",
        "communication_date": "2001-07-18",
        "intent": "announce",
        "subject_matter": "Announcement",
        "keywords": ["Announcement"],
        "demand_amount": None,
        "action_items": [],
        "urgency": "routine",
        "confidence": 0.9,
    }
    from pipeline.extraction_normalize import normalize_specialist_extraction

    normalized = normalize_specialist_extraction("correspondence", payload)
    assert validate_extraction("correspondence", normalized)["schema_valid"] is True


def test_cms_posthoc_extracts_notice_id_and_amount():
    from observability.posthoc_gt import extract_insurance_fields

    text = Path(__file__).parents[1].joinpath(
        "..", "data", "pipeline", "review", "inpatient_196101176981976_1.txt"
    ).resolve()
    if not text.is_file():
        pytest.skip("pilot review artifact not present")
    fields = extract_insurance_fields(text.read_text(encoding="utf-8"))
    assert fields["claim_number"] == "196101176981976"
    assert fields["claimed_amount"] == 9000.0
    assert any("4900GV" in doc for doc in fields.get("supporting_documents", []))


def test_full_coverage_high_confidence_still_reports():
    state = {
        "doc_type": "contract",
        "extraction_confidence": 0.91,
        "extraction_attempts": 1,
        "extracted_data": {
            "parties": ["Acme"],
            "governing_law": "Delaware",
            "effective_date": "2024-01-01",
        },
        "ground_truth": {
            "expected_fields": {
                "parties": ["Acme"],
                "governing_law": "Delaware",
                "effective_date": "2024-01-01",
            }
        },
    }
    assert after_extraction(state) == "compile_report"


def test_failed_compile_report_withholds_catalog():
    failed = {
        "doc_id": "d1",
        "report_error": True,
        "extracted_data": {
            "parties": ["A"],
            "_report": {"summary": "unavailable", "error": True},
        },
    }
    assert report_is_failed(failed) is True
    assert after_report(failed) == "human_review"
    ok = {
        "report_error": False,
        "extracted_data": {"parties": ["A"], "_report": {"summary": "ok"}},
    }
    assert after_report(ok) == "catalog_write"


def test_collect_review_causes_matches_visualizer_tokens():
    causes = collect_review_causes(
        {
            "doc_type": "contract",
            "ground_truth": {"expected_hf_class": "correspondence"},
        }
    )
    assert CLASS_MISS in causes
    assert HOLLOW_EXTRACTION not in causes
    assert "reconsider:" in (format_causes(causes) or "")
    hollow = collect_review_causes({"extracted_data": {"confidence": 0.9}})
    assert HOLLOW_EXTRACTION in hollow
    scored = collect_review_causes(scores={"extraction_overall_score": 0.41})
    assert EXTRACTION_MISS in scored
    report = collect_review_causes(
        {"extracted_data": {"_report": {"error": True}}, "report_error": True}
    )
    assert REPORTING_INCOMPLETE in report
    assert should_reconsider("archived", causes) is True
    assert should_reconsider("extract", causes) is False


def test_classify_graph_has_gt_miss_edge():
    from graph.build_graph import build_graph

    g = build_graph()
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("classify", "review_classify") in edges
    assert ("compile_report", "human_review") in edges
    assert ("compile_report", "catalog_write") in edges
