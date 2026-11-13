"""Grounded (Hub-labelled) runs score extractions against dataset labels only.

These tests follow the deployed path of an HF pilot run: a Hub ``ground_truth``
row is parsed by ``run_hf_pilot.parse_hf_row``, turned into the run's
``ground_truth`` by ``build_expected_fields``, and scored by the Dojo suite
through ``observability.suite_scoring``. Rows are synthetic but keep the Hub
shape (string cells, JSON text, ``"nan"`` placeholders).
"""

from __future__ import annotations

import json

import pytest

from observability.extraction_gt import (
    build_expected_fields,
    catalog_expected_fields,
    labelled_expected_fields,
)
from observability.suite_scoring import score_document_payload
from scripts.run_hf_pilot import parse_hf_row


def _sample(doc_class: str, subclass: str, text: str = "synthetic text", **labels):
    """One parsed HF pilot sample for a synthetic Hub ground-truth row."""
    gt = {"expected": doc_class, "expected_subclass": subclass, "expected_stage": "archived"}
    gt.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in labels.items()})
    row = {"filename": f"{doc_class}-{subclass}.txt", "doc_text": text, "expected": doc_class}
    sample = parse_hf_row(row, {row["filename"]: gt})
    assert sample is not None
    return sample


CORPORATE = {
    "intent": "governance_rules",
    "keywords": ["board", "resolution"],
    "subject_matter": "Board resolution approving a credit facility.",
    "entity_name": "Example Holdings Inc.",
}


@pytest.mark.parametrize(("subclass", "record_type"), [
    ("board_resolution", "other"),
    ("subsidiary_list", "other"),
    ("charter_amendment", "articles_of_incorporation"),
    ("bylaws", "bylaws"),
])
def test_corporate_record_type_gold_is_the_dataset_label(subclass, record_type):
    """The Hub ``record_type`` is the gold, never the raw subclass name."""
    sample = _sample("corporate_record", subclass, record_type=record_type, **CORPORATE)
    expected = catalog_expected_fields(sample)
    assert expected["record_type"] == record_type
    payload = score_document_payload("corporate_record", dict(expected), expected)
    assert payload["extraction"].overall_score == 1.0


def test_nan_label_is_not_expected_and_emits_no_zero():
    """A ``"nan"`` topic / sentiment cell is absence, so no 0.0 topic score."""
    labels = {
        "claim_number": "CLM-1", "policy_number": "POL-1", "insurer": "Example Mutual",
        "insured_party": "Jordan Example", "claim_type": "property",
        "coverage_determination": "approved", "content_topic": "nan", "sentiment_label": "nan",
    }
    sample = _sample("insurance_claim", "property", **labels)
    expected = catalog_expected_fields(sample)
    assert "content_topic" not in expected and "sentiment_label" not in expected
    payload = score_document_payload("insurance_claim", dict(expected), expected)
    assert payload.get("content_topic_accuracy") is None
    assert payload.get("sentiment_accuracy") is None
    assert payload["extraction"].overall_score == 1.0


def test_posthoc_regex_fills_are_routing_hints_not_gold():
    """Fields guessed from the text are kept for routing but never scored."""
    text = "From: Avery Example\nTo: Casey Example\nDate: March 3, 2024\nPlease review."
    sample = _sample("correspondence", "email", text=text, intent="other",
                     keywords=["review"], subject_matter="A request to review a draft.")
    fields, meta = build_expected_fields(sample)
    posthoc = {k for k, src in meta["sources"].items() if src == "posthoc"}
    assert posthoc, "the regex pass should fill at least one field from this text"
    ground_truth = {"expected_fields": fields, "expected_fields_sources": meta["sources"]}
    gold = labelled_expected_fields(ground_truth)
    assert set(gold) == set(fields) - posthoc
    assert {"intent", "keywords", "subject_matter"} <= set(gold)


def test_manifest_fields_without_sources_all_count():
    """An explicit manifest (no sources map) keeps every expected field."""
    gt = {"expected_fields": {"parties": ["A"], "governing_law": "Delaware"}}
    assert labelled_expected_fields(gt) == gt["expected_fields"]
    assert labelled_expected_fields(None) == {}


def test_hf_sample_carries_the_routing_gold():
    """``expected_stage`` from the Hub row reaches the sample (``stage_correct``)."""
    sample = _sample("corporate_record", "bylaws", record_type="bylaws", **CORPORATE)
    assert sample["expected_stage"] == "archived"


@pytest.mark.parametrize("error", [RuntimeError, KeyError, TypeError])
def test_scoring_failure_is_logged_not_swallowed(monkeypatch, error):
    """A Dojo crash inside scoring (any type) falls back to ``None`` but is logged."""
    from observability import suite_scoring

    events = []
    monkeypatch.setattr(suite_scoring.logger, "exception",
                        lambda event, **kw: events.append((event, kw)), raising=False)

    class Boom:
        def score_document(self, *a, **k):
            raise error("dojo bug")

    monkeypatch.setattr("llm_dojo_scoring.get_suite", lambda name: Boom())
    assert score_document_payload("contract", {}, {"parties": ["A"]}) is None
    assert events and events[0][0] == "score_document_payload_failed"


def test_unknown_class_returns_none_quietly(monkeypatch):
    """No live suite for the class is an expected case, not an error log."""
    from observability import suite_scoring

    events = []
    monkeypatch.setattr(suite_scoring.logger, "exception",
                        lambda event, **kw: events.append(event), raising=False)
    assert score_document_payload("no_such_class", {}, {"x": 1}) is None
    assert events == []


def test_pipeline_scores_only_labelled_fields(
    temp_base_dir, mock_openai_client, mock_langchain_llm, monkeypatch
):
    """``run_pipeline`` hands the field scorer the dataset labels, not regex fills."""
    from graph.build_graph import run_pipeline
    from observability import langfuse_field_scoring as lfs

    real = lfs.score_and_log_extraction
    captured = []

    def spy(trace_id, **kw):
        """Record the scorer call, then score for real."""
        captured.append(kw)
        return real(trace_id, **kw)

    monkeypatch.setattr(lfs, "score_and_log_extraction", spy)
    doc = temp_base_dir / "pipeline" / "inbox" / "grounded_labels.txt"
    doc.write_text("Service agreement between Acme Corp and Beta LLC.")
    mock_langchain_llm.classification = {
        "doc_type": "contract", "contract_subtype": "other", "confidence": 0.99,
        "reasoning": "Service agreement",
    }
    mock_langchain_llm.extraction = {
        "parties": ["Acme Corp", "Beta LLC"], "governing_law": "Delaware", "confidence": 0.99,
    }
    result = run_pipeline(
        doc,
        "MATTER-GT",
        ground_truth={
            "expected_doc_class": "contract",
            "expected_fields": {"parties": ["Acme Corp", "Beta LLC"], "governing_law": "Ohio"},
            "expected_fields_sources": {"parties": "hub", "governing_law": "posthoc"},
        },
    )
    assert result["stage"] == "archived"
    assert captured, "a grounded archived run must call the field scorer"
    assert captured[0]["expected"] == {"parties": ["Acme Corp", "Beta LLC"]}


def test_pilot_report_scores_only_labelled_fields():
    """The HF pilot's row score (and report re-scoring) ignore post-hoc fills.

    The prediction matches every dataset label and leaves the regex-guessed
    fields blank, so its labelled score is 1.0 whatever the regex guessed.
    """
    from scripts.run_hf_pilot import (
        enrich_sample_row,
        expected_fields_for_sample,
        expected_fields_meta,
        labelled_fields,
        score_row_extraction,
    )

    text = "From: Avery Example\nTo: Casey Example\nDate: March 3, 2024\nPlease review."
    labels = {"intent": "other", "keywords": ["review"],
              "subject_matter": "A request to review a draft."}
    sample = _sample("correspondence", "email", text=text, **labels)
    fields = expected_fields_for_sample(sample)
    sources = expected_fields_meta(sample)["sources"]
    assert any(src == "posthoc" for src in sources.values())
    predicted = {**labels, "communication_type": "email"}

    pilot = score_row_extraction(predicted, labelled_fields(fields, sources), "correspondence")
    assert pilot["overall_score"] == 1.0

    row = {"expected": "correspondence", "expected_subclass": "email",
           "predicted_subtype": "email", "extracted_data": predicted,
           "expected_fields": fields, "expected_fields_sources": sources}
    assert enrich_sample_row(row)["extraction_overall_score"] == 1.0
