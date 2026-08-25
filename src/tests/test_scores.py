import os


class TestValidateExtraction:
    def test_parse_error_flag(self):
        from observability.scores import validate_extraction

        result = validate_extraction("contract", {"_parse_error": True})
        assert result["parse_error"] is True
        assert result["schema_valid"] is False

    def test_valid_extraction(self):
        from observability.scores import validate_extraction

        result = validate_extraction(
            "contract",
            {
                "parties": ["ACME", "Zenith"],
                "effective_date": "2024-01-15",
                "term_length": "3 years",
                "termination_clauses": [],
                "governing_law": "Delaware",
                "key_obligations": ["uptime"],
                "contract_value": None,
                "renewal_terms": None,
            },
        )
        assert result["parse_error"] is False
        assert result["schema_valid"] is True

    def test_invalid_type_marks_schema_invalid(self):
        from observability.scores import validate_extraction

        # parties must be a list; a string should fail validation
        result = validate_extraction("contract", {"parties": "ACME"})
        assert result["schema_valid"] is False

    def test_unknown_doc_type_is_not_schema_valid(self):
        from observability.scores import validate_extraction

        result = validate_extraction("not_a_real_type", {"foo": 1})
        assert result["schema_valid"] is False
        result_unknown = validate_extraction("unknown", {"_unsupported": True})
        assert result_unknown["schema_valid"] is False

    def test_merger_agreement_validates_against_contract_schema(self):
        from observability.scores import validate_extraction

        result = validate_extraction(
            "merger_agreement",
            {
                "parties": ["Parent Inc.", "Target Corp."],
                "effective_date": "2024-06-01",
                "governing_law": "Delaware",
            },
        )
        assert result["parse_error"] is False
        assert result["schema_valid"] is True


class TestEmitPipelineScores:
    def test_scores_computed_when_tracing_disabled(self):
        # Scores are ALWAYS computed (persisted to the catalog even without a
        # tracing backend); only trace attachment is backend-gated.
        os.environ["OBSERVABILITY_PROVIDER"] = "none"
        try:
            from observability.scores import emit_pipeline_scores

            scores = emit_pipeline_scores(
                {
                    "doc_id": "d1",
                    "stage": "archived",
                    "doc_type": "contract",
                    "classification_confidence": 0.9,
                    "extracted_data": {"parties": ["A"]},
                },
                metrics={
                    "run_aborted": 0,
                    "run_duration_seconds": 1.5,
                    "total_tokens": 42,
                    "llm_call_count": 3,
                    "estimated_cost_usd": 0.001,
                    "classification_attempts": 1,
                    "extraction_attempts": 1,
                },
            )
            assert scores["stage_completed"] == 1
            assert scores["parse_error"] == 0
            assert scores["schema_valid"] == 1
            assert scores["classification_confidence"] == 0.9
            assert scores["run_aborted"] == 0
            assert scores["total_tokens"] == 42
            assert scores["llm_call_count"] == 3
            assert scores["estimated_cost_usd"] == 0.001
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)


def test_langfuse_score_name_aliases_overlong_verified_precision():
    from observability.scores import langfuse_score_name

    assert langfuse_score_name("extraction_overall_verified_precision") == (
        "extraction_verified_precision"
    )
    assert langfuse_score_name("run_duration_seconds") == "run_duration_seconds"
    assert len("extraction_verified_precision") <= 35

