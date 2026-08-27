"""Wire extraction_category_presence from existing CUAD ground truth."""

import os

from llm_dojo_scoring.field_scoring import score_category_presence
from observability.extraction_gt import presence_expectations_from_ground_truth
from observability.field_scoring import get_field_types


def test_presence_from_cuad_clause_lines_scores_match():
    gt = {
        "expected_doc_class": "contract",
        "expected_fields": {
            "cuad_clauses": ["Anti-Assignment: not assignable without consent"],
        },
    }
    presence = presence_expectations_from_ground_truth(gt, "contract")
    assert presence is not None
    assert presence["Anti-Assignment"]["expected"] is True
    assert "not assignable" in presence["Anti-Assignment"]["answer"]
    assert presence["Anti-Assignment"]["field"] == "cuad_clauses"
    assert presence["Governing Law"]["expected"] is False

    predicted = {
        "cuad_clauses": ["Anti-Assignment: not assignable without consent"],
    }
    score, detail = score_category_presence(
        predicted, presence, get_field_types("contract")
    )
    assert score == 1.0
    assert detail["Anti-Assignment"]["matched"] is True


def test_presence_missing_expected_clause_is_below_one():
    gt = {
        "expected_doc_class": "contract",
        "expected_fields": {
            "cuad_clauses": ["Governing Law: State of Delaware"],
        },
    }
    presence = presence_expectations_from_ground_truth(gt, "contract")
    predicted = {"cuad_clauses": ["Parties: Acme Corp"]}
    score, _ = score_category_presence(
        predicted, presence, get_field_types("contract")
    )
    assert score < 1.0


def test_no_cuad_gt_returns_none():
    assert presence_expectations_from_ground_truth(
        {"expected_doc_class": "correspondence", "expected_fields": {"sender": "A"}},
        "correspondence",
    ) is None
    assert presence_expectations_from_ground_truth(
        {"expected_doc_class": "contract", "expected_fields": {"parties": ["A"]}},
        "contract",
    ) is None


def test_hub_label_map_empty_span_is_expected_false():
    gt = {
        "expected_doc_class": "contract",
        "cuad_clause_labels": {
            "Anti-Assignment": [{"start": 0, "text": "shall not assign"}],
            "Governing Law": [],
        },
    }
    presence = presence_expectations_from_ground_truth(gt, "contract")
    assert presence["Anti-Assignment"]["expected"] is True
    assert presence["Governing Law"]["expected"] is False


def test_score_and_log_omits_category_presence_without_expectations():
    os.environ["OBSERVABILITY_PROVIDER"] = "none"
    try:
        from unittest.mock import patch
        from observability.langfuse_field_scoring import score_and_log_extraction

        with patch("observability.scores.is_enabled", return_value=True), patch(
            "observability.scores.create_trace_score"
        ) as scored:
            score_and_log_extraction(
                trace_id="t-cat",
                doc_class="contract",
                field_types=get_field_types("contract"),
                predicted={"effective_date": "2020-01-02"},
                expected={"effective_date": "2020-01-02"},
            )
            names = [c.kwargs.get("name") for c in scored.call_args_list]
            assert "extraction_category_presence" not in names
    finally:
        os.environ.pop("OBSERVABILITY_PROVIDER", None)


def test_score_and_log_emits_category_presence_when_wired():
    os.environ["OBSERVABILITY_PROVIDER"] = "none"
    try:
        from unittest.mock import patch
        from observability.langfuse_field_scoring import score_and_log_extraction

        presence = presence_expectations_from_ground_truth(
            {
                "expected_doc_class": "contract",
                "expected_fields": {
                    "cuad_clauses": ["Parties: Acme Corp and Beta LLC"],
                },
            },
            "contract",
        )
        with patch("observability.scores.is_enabled", return_value=True), patch(
            "observability.scores.create_trace_score"
        ) as scored:
            score_and_log_extraction(
                trace_id="t-cat-2",
                doc_class="contract",
                field_types=get_field_types("contract"),
                predicted={"cuad_clauses": ["Parties: Acme Corp and Beta LLC"]},
                expected={"parties": ["Acme Corp"]},
                presence_expectations=presence,
            )
            names = [c.kwargs.get("name") for c in scored.call_args_list]
            assert "extraction_category_presence" in names
            cat = next(
                c.kwargs for c in scored.call_args_list
                if c.kwargs.get("name") == "extraction_category_presence"
            )
            assert cat["value"] == 1.0
    finally:
        os.environ.pop("OBSERVABILITY_PROVIDER", None)
