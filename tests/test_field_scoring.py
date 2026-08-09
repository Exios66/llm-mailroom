"""Deterministic field-type-aware extraction scoring (issues #4/#5).

Covers the per-type scorers in observability/field_scoring.py, the composite
score_extraction(), the config-driven field_types mapping, and the judge-gating
behavior of graph/build_graph.py:_emit_pipeline_result.

No real embedding model is ever loaded in tests: the `_get_embedding` hook is
monkeypatched to None by default so scoring is pure string-based, and the
embedding-rescue tests inject a fake similarity function.
"""

import os
from unittest.mock import patch

import pytest

from observability import field_scoring
from observability.field_scoring import (
    FIELD_SCORERS,
    EntityListScore,
    ExtractionScoreResult,
    get_field_types,
    normalize_text,
    score_date_field,
    score_extraction,
    score_field,
    score_entity_list,
    score_free_text_field,
    score_id_field,
    score_money_field,
    score_name_field,
)


@pytest.fixture(autouse=True)
def _no_real_embedding(monkeypatch):
    """Never load sentence-transformers in tests (no model download)."""
    monkeypatch.setattr(field_scoring, "_get_embedding", lambda: None)


class FakeEmbedding:
    """Deterministic fake embedding similarity for rescue tests."""

    def __init__(self, sim: float):
        self._sim = sim

    def similarity(self, a, b):
        return self._sim


class TestNormalize:
    def test_suffix_strip_and_punct(self):
        assert normalize_text("Global Technologies, Ltd") == "GLOBAL TECHNOLOGIES"
        assert normalize_text("global technologies ltd") == "GLOBAL TECHNOLOGIES"

    def test_whitespace_collapse(self):
        assert normalize_text("  John   Smith  ") == "JOHN SMITH"

    def test_esq_stripped(self):
        assert normalize_text("John Smith, Esq.") == "JOHN SMITH"


class TestDateField:
    def test_format_equivalence_scores_one(self):
        assert score_date_field("March 3, 2024", "03/03/2024") == 1.0
        assert score_date_field("2024-03-03", "03/03/2024") == 1.0
        assert score_date_field("March 3rd, 2024", "2024-03-03") == 1.0

    def test_wrong_date_scores_zero(self):
        assert score_date_field("2024-03-04", "2024-03-03") == 0.0

    def test_unparseable_falls_back_to_fuzzy(self):
        # Both unparseable: fuzzy string comparison, not a hard 0.
        s = score_date_field("see renewal schedule", "renewal schedule")
        assert 0.0 < s <= 1.0


class TestMoneyField:
    def test_format_equivalence(self):
        assert score_money_field("$250,000", "250000.00") == 1.0
        assert score_money_field("$1.2M", "1,200,000") == 1.0

    def test_wrong_amount_scores_zero(self):
        assert score_money_field("$250,001", "$250,000") == 0.0

    def test_numeric_inputs(self):
        assert score_money_field(250000, "250000.00") == 1.0

    def test_prose_falls_back_to_fuzzy(self):
        exp = "Tiered or flat commission based on private offer terms"
        pred = "Tiered or flat commission based on private offer terms"
        assert score_money_field(pred, exp) == 1.0
        s = score_money_field("flat commission based on private offer terms", exp)
        assert 0.0 < s <= 1.0


class TestIdField:
    def test_exact_after_normalize(self):
        assert score_id_field("001-39420", "001-39420") == 1.0
        assert score_id_field("Sec-42", "SEC 42") == 1.0

    def test_wrong_id_scores_zero(self):
        assert score_id_field("001-39420", "001-3942X") == 0.0


class TestNameField:
    def test_suffix_variants_score_one(self):
        assert score_name_field("Global Technologies, Ltd", "Global Technologies Ltd") == 1.0
        assert score_name_field("Chase Bank USA, N.A.", "Chase Bank USA, N.A.") == 1.0

    def test_order_variants_score_high(self):
        assert score_name_field("John Smith, Esq.", "Smith, John") > 0.7
        assert score_name_field("John A. Smith", "John Smith") > 0.7

    def test_disjoint_names_score_low(self):
        assert score_name_field("Acme Corp", "Northwind Logistics") < 0.5

    def test_embedding_rescues_lexically_distant(self):
        with patch.object(field_scoring, "_get_embedding", return_value=FakeEmbedding(0.92)):
            assert score_name_field("Acme Corp", "Northwind Logistics") == 0.92

    def test_embedding_never_overrides_strong_string_score(self):
        with patch.object(field_scoring, "_get_embedding", return_value=FakeEmbedding(0.05)):
            assert score_name_field("Global Technologies, Ltd", "Global Technologies Ltd") == 1.0


class TestFreeTextField:
    def test_identical_scores_one(self):
        assert score_free_text_field(
            "Company may terminate for convenience upon 30 days' notice",
            "Company may terminate for convenience upon 30 days' notice",
        ) == 1.0

    def test_paraphrase_shares_tokens(self):
        s = score_free_text_field(
            "Either party may end this agreement at any time",
            "Either Party shall have the right to terminate this Agreement at any time",
        )
        assert 0.3 < s < 1.0

    def test_disjoint_scores_zero(self):
        assert score_free_text_field("termination at will", "quarterly compliance reports") == 0.0

    def test_embedding_rescues_paraphrase(self):
        with patch.object(field_scoring, "_get_embedding", return_value=FakeEmbedding(0.88)):
            assert score_free_text_field("terminate for convenience", "termination without cause") == 0.88


class TestEntityList:
    def test_reordered_list_scores_one(self):
        el = score_entity_list("name", ["Alice", "Bob", "Carol"], ["Carol", "Alice", "Bob"])
        assert el.f1 == 1.0 and el.precision == 1.0 and el.recall == 1.0
        assert el.matched == 3 and el.unmatched_predicted == 0 and el.unmatched_expected == 0

    def test_missing_item_hits_recall(self):
        el = score_entity_list("name", ["Alice", "Bob"], ["Alice", "Bob", "Carol"])
        assert el.recall == 2 / 3 and el.precision == 1.0
        assert el.matched == 2 and el.unmatched_expected == 1

    def test_extra_item_hits_precision(self):
        el = score_entity_list("name", ["Alice", "Bob", "Dave"], ["Alice", "Bob"])
        assert el.precision == 2 / 3 and el.recall == 1.0
        assert el.unmatched_predicted == 1

    def test_empty_sides(self):
        assert score_entity_list("name", [], []).f1 == 1.0
        assert score_entity_list("name", ["Alice"], []).f1 == 0.0
        assert score_entity_list("name", [], ["Alice"]).f1 == 0.0

    def test_id_elements_exact(self):
        el = score_entity_list("id", ["530 U.S. 428", "541 U.S. 545"], ["541 U.S. 545", "530 U.S. 428"])
        assert el.f1 == 1.0

    def test_off_by_one_similarity(self):
        # "Smith, John" vs "John Smith" pairs match; order does not matter.
        el = score_entity_list("name", ["John Smith", "Jane Doe"], ["Smith, John", "Doe, Jane"])
        assert el.f1 == 1.0

    def test_returns_typed_object(self):
        assert isinstance(score_entity_list("name", ["A"], ["A"]), EntityListScore)


class TestDispatch:
    def test_scalar_dispatch(self):
        assert score_field("date", "2024-03-03", "03/03/2024") == 1.0
        assert score_field("id", "SEC-42", "SEC 42") == 1.0

    def test_entity_list_dispatch(self):
        result = score_field("entity_list:name", ["Alice", "Bob"], ["Bob", "Alice"])
        assert isinstance(result, EntityListScore) and result.f1 == 1.0

    def test_unknown_type_falls_back_to_name(self):
        assert 0.0 <= score_field("bogus_type", "x", "y") <= 1.0

    def test_heuristic_field_type(self):
        assert field_scoring._heuristic_field_type("filing_date", "2024-01-01") == "date"
        assert field_scoring._heuristic_field_type("contract_value", "5") == "money"
        assert field_scoring._heuristic_field_type("docket_number", "42") == "id"
        assert field_scoring._heuristic_field_type("parties", ["A"]) == "entity_list"
        assert field_scoring._heuristic_field_type("subject", "hi") == "name"


class TestFieldTypesFromConfig:
    def test_taxonomy_mapping_for_contract(self):
        ft = get_field_types("contract")
        assert ft["effective_date"] == "date"
        assert ft["contract_value"] == "money"
        assert ft["parties"] == "entity_list:name"
        assert ft["termination_clauses"] == "entity_list:free_text"

    def test_unknown_class_returns_empty(self):
        assert get_field_types("not_a_class") == {}


class TestScoreExtraction:
    def test_perfect_extraction_scores_one(self):
        expected = {
            "parties": ["Chase Bank USA, N.A.", "Affiliate"],
            "effective_date": "2020-01-02",
            "contract_value": "$250,000",
            "governing_law": "Florida",
            "termination_clauses": ["Either party may terminate with notice"],
        }
        result = score_extraction("contract", get_field_types("contract"), dict(expected), expected)
        assert isinstance(result, ExtractionScoreResult)
        assert result.overall_score == 1.0
        assert result.field_scores == {k: 1.0 for k in expected}
        assert result.ambiguous_fields == []
        assert not result.needs_judge_review
        assert set(result.entity_list_scores) == {"parties", "termination_clauses"}

    def test_null_expected_fields_not_scored(self):
        expected = {"effective_date": "2020-01-02", "renewal_terms": None}
        result = score_extraction("contract", get_field_types("contract"), {"effective_date": "2020-01-02"}, expected)
        assert result.overall_score == 1.0
        assert result.field_scores == {"effective_date": 1.0}

    def test_missing_predicted_field_scores_zero(self):
        expected = {"effective_date": "2020-01-02", "governing_law": "Delaware"}
        result = score_extraction("contract", get_field_types("contract"), {}, expected)
        assert result.overall_score == 0.0
        assert result.needs_judge_review is False  # clearly wrong, not ambiguous

    def test_ambiguous_band_triggers_judge_review(self):
        expected = {"governing_law": "Delaware", "term_length": "one year"}
        predicted = {"governing_law": "Delaware", "term_length": "one (1) year"}
        result = score_extraction("contract", get_field_types("contract"), predicted, expected)
        assert result.ambiguous_fields
        assert result.needs_judge_review is True

    def test_unmapped_field_uses_heuristic(self):
        result = score_extraction(
            "contract", {}, {"something_date": "2024-01-01"}, {"something_date": "2024-01-01"}
        )
        assert result.overall_score == 1.0

    def test_entity_list_detail_available(self):
        expected = {"parties": ["Alice", "Bob", "Carol"]}
        predicted = {"parties": ["Alice", "Bob"]}
        result = score_extraction("contract", {"parties": "entity_list:name"}, predicted, expected)
        el = result.entity_list_scores["parties"]
        assert el.recall == pytest.approx(2 / 3)
        assert el.unmatched_expected == 1


class TestLangfuseWiring:
    def test_score_configs_registered(self):
        from observability.langfuse_field_scoring import FIELD_SCORE_CONFIGS
        from observability.scores import SCORE_CONFIGS

        names = {c["name"] for c in SCORE_CONFIGS}
        for cfg in FIELD_SCORE_CONFIGS:
            assert cfg["name"] in names

    def test_score_and_log_noops_when_disabled(self):
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        try:
            from observability.langfuse_field_scoring import score_and_log_extraction

            result = score_and_log_extraction(
                trace_id="t1",
                doc_class="contract",
                field_types=get_field_types("contract"),
                predicted={"effective_date": "2020-01-02"},
                expected={"effective_date": "2020-01-02"},
            )
            assert result.overall_score == 1.0
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)


class TestJudgeGating:
    """graph/build_graph.py:_emit_pipeline_result suppression."""

    def _fake_observation(self, calls):
        class FakeObs:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

            def update(self, **kw):
                calls.append(kw)

        return FakeObs()

    def test_suppressed_for_unambiguous_verdict(self):
        from graph import build_graph as bg

        calls = []
        with patch.object(bg, "observation", return_value=self._fake_observation(calls)):
            bg._emit_pipeline_result(
                object(), {"stage": "archived", "doc_type": "contract"}, {}, judge_required=False
            )
        assert calls == []

    def test_emitted_for_ambiguous_verdict(self):
        from graph import build_graph as bg

        calls = []
        with patch.object(bg, "observation", return_value=self._fake_observation(calls)):
            bg._emit_pipeline_result(
                {"stage": "archived", "doc_type": "contract"},
                {"stage": "archived", "doc_type": "contract"},
                {"ground_truth": {"expected_fields": {"x": 1}}},
                judge_required=True,
            )
        assert len(calls) == 1

    def test_emitted_for_live_runs(self):
        from graph import build_graph as bg

        calls = []
        with patch.object(bg, "observation", return_value=self._fake_observation(calls)):
            bg._emit_pipeline_result(
                {"stage": "archived", "doc_type": "contract"},
                {"stage": "archived", "doc_type": "contract"},
                {},
                judge_required=None,
            )
        assert len(calls) == 1
