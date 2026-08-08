from graph.routing import (
    after_classify,
    after_extraction,
    after_boss,
    after_human_review,
)


class TestRoutingLogic:
    def test_after_classify_high_confidence_routes_to_extract(self):
        state = {
            "classification_confidence": 0.95,
            "classification_attempts": 1,
            "doc_type": "contract",
        }
        assert after_classify(state) == "extract"

    def test_after_classify_medium_confidence_routes_to_review(self):
        # Medium band (low <= confidence < high): classified but not clearly
        # confident (e.g. multi-topic memo) -> human review, never auto-archive.
        state = {
            "classification_confidence": 0.90,
            "classification_attempts": 1,
            "doc_type": "correspondence",
        }
        assert after_classify(state) == "human_review"

    def test_after_classify_medium_confidence_exhausts_no_retry_budget(self):
        # The medium band routes straight to review; it must not be treated as
        # a sub-low failure that burns the retry budget.
        state = {
            "classification_confidence": 0.80,
            "classification_attempts": 0,
            "doc_type": "contract",
        }
        assert after_classify(state) == "human_review"

    def test_after_classify_low_confidence_first_attempt_retry(self):
        state = {
            "classification_confidence": 0.50,
            "classification_attempts": 1,
            "doc_type": "contract",
        }
        assert after_classify(state) == "retry_classify"

    def test_after_classify_low_confidence_max_retries_review(self):
        state = {
            "classification_confidence": 0.50,
            "classification_attempts": 2,
            "doc_type": "contract",
        }
        assert after_classify(state) == "human_review"

    def test_after_classify_unknown_type_review(self):
        state = {
            "classification_confidence": 0.80,
            "classification_attempts": 1,
            "doc_type": "nonexistent_type",
        }
        assert after_classify(state) == "human_review"

    def test_after_extraction_high_confidence_routes_to_report(self):
        state = {
            "extraction_confidence": 0.90,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "compile_report"

    def test_after_extraction_conflict_routes_to_boss(self):
        state = {
            "extraction_confidence": 0.90,
            "extraction_attempts": 1,
            "conflict_detected": True,
        }
        assert after_extraction(state) == "boss_escalation"

    def test_after_extraction_low_confidence_first_retry(self):
        state = {
            "extraction_confidence": 0.50,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "retry_extract"

    def test_after_extraction_low_confidence_max_retries_review(self):
        state = {
            "extraction_confidence": 0.50,
            "extraction_attempts": 2,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "human_review"

    def test_after_extraction_schema_invalid_first_attempt_retry(self):
        # High stated confidence must NOT archive a schema-invalid extraction.
        state = {
            "doc_type": "contract",
            "extracted_data": {"parties": "not-a-list"},
            "extraction_confidence": 0.95,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "retry_extract"

    def test_after_extraction_schema_invalid_max_attempts_review(self):
        state = {
            "doc_type": "contract",
            "extracted_data": {"parties": "not-a-list"},
            "extraction_confidence": 0.95,
            "extraction_attempts": 2,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "human_review"

    def test_after_extraction_parse_error_first_attempt_retry(self):
        state = {
            "doc_type": "contract",
            "extracted_data": {"_parse_error": True, "_raw": "not json"},
            "extraction_confidence": 0.30,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "retry_extract"

    def test_after_extraction_schema_valid_high_confidence_ignores_extra_keys(self):
        # Specialists embed instructions in the prompt; pydantic ignores extra
        # keys like `_unsupported` / `reasoning` — must still route to report.
        state = {
            "doc_type": "contract",
            "extracted_data": {"parties": ["Acme"], "_unsupported": True, "reasoning": "x"},
            "extraction_confidence": 0.90,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "compile_report"

    def test_after_boss_approved_routes_to_report(self):
        state = {"review_decision": "approved"}
        assert after_boss(state) == "compile_report"

    def test_after_boss_review_routes_to_human(self):
        state = {"review_decision": "review"}
        assert after_boss(state) == "human_review"

    def test_after_human_review_approved(self):
        state = {"review_decision": "approved"}
        assert after_human_review(state) == "compile_report"

    def test_after_human_review_rejected(self):
        state = {"review_decision": "rejected"}
        assert after_human_review(state) == "failed"
