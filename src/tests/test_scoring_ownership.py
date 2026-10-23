"""Source-only regressions for canonical Dojo delegation and report shapes."""

import pytest

from llm_dojo_scoring import EntityListScore

from llm_dojo_scoring import bootstrap as dojo_bootstrap
from llm_dojo_scoring import diagnostics as dojo_diagnostics
from observability import bootstrap, metrics


def test_bootstrap_facade_calls_the_canonical_engine():
    """Copied formulas fail the identity check; real calls pin the old contract."""
    assert bootstrap.bootstrap_ci is dojo_bootstrap.bootstrap_ci
    assert bootstrap.delta_significance is dojo_bootstrap.delta_significance
    assert bootstrap.bootstrap_ci([None, 1]) is None
    assert bootstrap.bootstrap_ci([1, 1, 1], n_boot=40, seed=7) == {
        "lo": 1.0, "hi": 1.0, "half": 0.0, "n": 3, "seed": 7,
        "n_boot": 40, "method": "percentile-bootstrap",
    }
    assert bootstrap.delta_significance([0, 0], [1, 1], n_boot=40, seed=7) == {
        "delta": 1.0, "ci_lo": 1.0, "ci_hi": 1.0, "significant": True,
        "n_a": 2, "n_b": 2, "seed": 7, "n_boot": 40,
        "method": "two-sample-percentile-bootstrap",
    }


def test_diagnostics_delegates_real_rows_and_preserves_report_keys(monkeypatch):
    """Hand-counted examples pin populations, values, and one real Dojo call."""
    calls = []
    real = dojo_diagnostics.extraction_diagnostics

    def tracked(rows, field_types):
        """Observe the canonical call without replacing its calculation."""
        calls.append((rows, field_types))
        return real(rows, field_types)

    monkeypatch.setattr(dojo_diagnostics, "extraction_diagnostics", tracked)
    rows = [
        {"predicted": {"effective_date": "2024-01-02", "amount": "110",
                       "term_length": "1 year"},
         "expected_fields": {"effective_date": "2024-01-01", "amount": "100",
                             "term_length": "2 years"},
         "entity_list_scores": {"cuad_clauses": EntityListScore(
             field_name="cuad_clauses", precision=0.5, recall=1.0, f1=0.6667,
             matched=1, unmatched_predicted=1, unmatched_expected=0)}},
        {"predicted": {"effective_date": "2024-01-04", "amount": "180",
                       "term_length": "2 years"},
         "expected_fields": {"effective_date": "2024-01-03", "amount": "200",
                             "term_length": "3 years"},
         "entity_list_scores": {"cuad_clauses": {
             "precision": 1.0, "recall": 0.5, "f1": 0.6667, "matched": 1,
             "n_predicted": 1, "n_expected": 2}}},
    ]
    types = {"effective_date": "date", "amount": "money",
             "term_length": "free_text", "cuad_clauses": "entity_list:free_text"}
    result = metrics.extraction_diagnostics(rows, types, master={"unused": True})
    assert len(calls) == 1
    assert isinstance(calls[0][0][0]["entity_list_scores"]["cuad_clauses"], dict)
    assert isinstance(rows[0]["entity_list_scores"]["cuad_clauses"], EntityListScore)
    assert result["date_n_pairs"] == 2
    assert result["date_mae_days"] == 1.0
    assert result["date_r2"] == 0.0
    assert result["duration_mae_days"] == 365.0
    assert result["duration_r2"] == -3.0
    assert result["money_mae_usd"] == 15.0
    assert result["list_precision"] == 0.75
    assert result["list_recall"] == 0.75
    assert result["list_micro_precision"] == pytest.approx(0.6667)
    assert result["list_micro_recall"] == pytest.approx(0.6667)
    assert result["span_count_mae"] == 1.0
    assert result["span_count_signed_mean"] == 0.0
    assert "list_headline_field" not in result


def test_real_entity_list_score_keeps_canonical_event_counts():
    """The actual Dojo object has unmatched counts, not n_predicted fields."""
    score = EntityListScore(
        field_name="cuad_clauses", precision=0.5, recall=1.0, f1=0.6667,
        matched=1, unmatched_predicted=1, unmatched_expected=0)
    result = metrics.extraction_diagnostics(
        [{"entity_list_scores": {"cuad_clauses": score}}],
        {"cuad_clauses": "entity_list:free_text"})
    assert result["list_micro_n_predicted"] == 2
    assert result["list_micro_n_expected"] == 1
    assert result["list_micro_matched"] == 1
    assert result["list_micro_precision"] == 0.5
    assert result["list_micro_recall"] == 1.0
    assert result["span_count_mae"] == 1.0


def test_diagnostics_keeps_non_cuad_headlines_absent_and_empty_support():
    """Dojo's broader headline choice cannot change the pipeline JSON shape."""
    rows = [{"entity_list_scores": {"keywords": {
        "precision": 1.0, "recall": 1.0, "f1": 1.0,
        "matched": 1, "n_predicted": 1, "n_expected": 1}}}]
    result = metrics.extraction_diagnostics(rows, {"keywords": "entity_list:name"})
    assert result["entity_list_precision"] == {"keywords": 1.0}
    assert "list_precision" not in result
    assert "list_headline_field" not in result
    empty = metrics.extraction_diagnostics([], {})
    assert empty["date_n_pairs"] == 0
    assert empty["duration_n_pairs"] == 0
    assert empty["money_n_pairs"] == 0
    assert empty["field_exact_rate"] == 0.0
