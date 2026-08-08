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

    def test_unknown_doc_type_is_valid(self):
        from observability.scores import validate_extraction

        result = validate_extraction("not_a_real_type", {"foo": 1})
        assert result["schema_valid"] is True


class TestEmitPipelineScores:
    def test_noop_when_tracing_disabled(self):
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
                }
            )
            assert scores == {}
        finally:
            os.environ.pop("OBSERVABILITY_PROVIDER", None)
