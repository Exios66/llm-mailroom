"""Dedicated specialist scoring suites from llm-dojo-scoring 0.9.0."""

from schemas.documents import CorrespondenceExtraction


def test_correspondence_schema_does_not_include_enron_differentiators():
    fields = CorrespondenceExtraction.model_fields
    assert "content_topic" not in fields
    assert "sentiment_label" not in fields
    assert "communication_type" in fields


def test_get_suite_rebinds_merger_away_from_cuad():
    from llm_dojo_scoring import get_suite

    merger = get_suite("merger_agreement")
    contract = get_suite("contract")
    assert merger.doc_type == "merger_agreement"
    assert "all_cash" in merger.subclasses
    assert "license" not in merger.subclasses
    assert "affiliate" in contract.subclasses
    assert "all_cash" not in contract.subclasses


def test_correspondence_suite_scores_topic_as_extra_not_extraction_field():
    from llm_dojo_scoring import get_suite
    from llm_dojo_scoring.content_scoring import peel_non_extraction_fields

    expected = {
        "sender": "a@b.com",
        "recipient": "c@d.com",
        "communication_type": "email",
        "content_topic": "legal",
        "sentiment_label": "neutral",
    }
    predicted = dict(expected)
    peeled_exp, peeled_pred, payload = peel_non_extraction_fields(expected, predicted)
    assert "content_topic" not in peeled_exp
    assert "sentiment_label" not in peeled_pred
    assert payload["content_topic"] == ("legal", "legal")

    out = get_suite("correspondence_specialist").score(expected, predicted)
    assert out["content_topic_accuracy"] == 1.0
    assert out["sentiment_accuracy"] == 1.0
    assert out["extraction"].overall_score == 1.0
    assert "content_topic" not in out["extraction"].field_scores


def test_merger_suite_emits_maud_question_extras():
    from llm_dojo_scoring import get_suite

    expected = {
        "parties": ["A", "B"],
        "maud_clause_labels": {"Type of Consideration": "All Cash"},
    }
    predicted = {
        "parties": ["A", "B"],
        "maud_clauses": ["Type of Consideration: All Cash"],
        "maud_clause_labels": {"Type of Consideration": "All Cash"},
    }
    out = get_suite("merger_agreement").score(expected, predicted)
    assert "maud_question_accuracy" in out
    assert out["maud_question_accuracy"] == 1.0


def test_score_with_suite_unwraps_extras():
    from observability.suite_scoring import score_with_suite

    expected = {
        "sender": "Pat",
        "recipient": "Kim",
        "communication_type": "email",
        "content_topic": "legal",
        "sentiment_label": "neutral",
    }
    result, extras = score_with_suite(
        "correspondence",
        expected,
        expected,
        field_types={"sender": "name", "recipient": "name", "communication_type": "name"},
    )
    assert result.overall_score == 1.0
    assert extras["content_topic_accuracy"] == 1.0
    assert extras["sentiment_accuracy"] == 1.0


def test_subclass_ok_prefers_sorter_doc_subclass():
    from scripts.run_hf_pilot import subclass_ok

    assert subclass_ok(
        "correspondence",
        "email",
        predicted_subtype="email",
        extracted={"communication_type": "letter"},
    ) is True
    assert subclass_ok(
        "merger_agreement",
        "all_cash",
        predicted_subtype="all_cash",
        extracted={},
    ) is True
    assert subclass_ok(
        "corporate_record",
        "bylaws",
        predicted_subtype="bylaws",
        extracted={"record_type": "other"},
    ) is True


def test_score_configs_include_suite_extras():
    from observability.scores import SCORE_CONFIGS
    from observability.suite_scoring import SUITE_EXTRA_SCORE_NAMES

    names = {c["name"] for c in SCORE_CONFIGS}
    assert SUITE_EXTRA_SCORE_NAMES <= names
