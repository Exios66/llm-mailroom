from graph.routing import (
    after_classify,
    after_extraction,
    after_boss,
    after_human_review,
    after_retry_classify,
    after_retry_extraction,
    after_review_classify,
    after_arbiter,
    after_judge,
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
        # confident (e.g. multi-topic memo). L-9: the first medium-confidence
        # result gets ONE re-classification pass; only after that retry does it
        # go to human review.
        state = {
            "classification_confidence": 0.90,
            "classification_attempts": 1,
            "doc_type": "correspondence",
        }
        assert after_classify(state) == "retry_classify"
        exhausted = {**state, "classification_attempts": 2}
        assert after_classify(exhausted) == "human_review"

    def test_after_classify_medium_confidence_exhausts_no_retry_budget(self):
        # The medium band uses the confidence retry budget (L-9) — a fresh doc
        # with 0 attempts gets its re-classification pass, and once the budget
        # is spent it routes to review.
        state = {
            "classification_confidence": 0.80,
            "classification_attempts": 0,
            "doc_type": "contract",
        }
        assert after_classify(state) == "retry_classify"
        assert after_classify({**state, "classification_attempts": 2}) == "human_review"

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

    def test_after_classify_unknown_token_high_confidence_parks(self):
        # Doctrine emits `unknown` for court opinions / DD memos. That token
        # is NOT a specialist class — even 0.99 must never extract.
        state = {
            "classification_confidence": 0.99,
            "classification_attempts": 1,
            "doc_type": "unknown",
        }
        assert after_classify(state) == "human_review"

    def test_after_classify_retired_class_high_confidence_parks(self):
        state = {
            "classification_confidence": 0.99,
            "classification_attempts": 1,
            "doc_type": "court_opinion",
        }
        assert after_classify(state) == "human_review"

    def test_after_classify_merger_agreement_high_confidence_extracts(self):
        # Extract alias: sorter may emit merger_agreement; contracts specialist
        # owns the schema. Must not park the way retired classes do.
        state = {
            "classification_confidence": 0.99,
            "classification_attempts": 1,
            "doc_type": "merger_agreement",
        }
        assert after_classify(state) == "extract"
        assert after_retry_classify(state) == "extract"
        assert after_review_classify({
            "review_verdict": "reviewer_agrees_high",
            "reviewer_confidence": 0.99,
            "reviewer_doc_type": "merger_agreement",
        }) == "extract"

    def test_after_classify_unknown_type_review(self):
        state = {
            "classification_confidence": 0.80,
            "classification_attempts": 1,
            "doc_type": "nonexistent_type",
        }
        assert after_classify(state) == "human_review"

    def test_after_classify_missing_type_review(self):
        # Empty type used to skip the unknown-type arm (`if doc_type and …`)
        # and fall through to extract at high confidence.
        state = {
            "classification_confidence": 0.98,
            "classification_attempts": 1,
            "doc_type": "",
        }
        assert after_classify(state) == "human_review"
        assert after_classify({**state, "doc_type": None}) == "human_review"

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
        # keys like `_unsupported` / an unknown blob — must still route to
        # report. `reasoning` is now a real schema field (v24+ trace dict).
        state = {
            "doc_type": "contract",
            "extracted_data": {
                "parties": ["Acme"],
                "_unsupported": True,
                "reasoning": {"summary": "s", "entries": []},
                "unknown_blob": "ignored",
            },
            "extraction_confidence": 0.90,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "compile_report"

    def test_after_extraction_unsupported_stub_skips_retry(self):
        # Missing-specialist stub used to look like a low-confidence extract
        # and burn retry_extract on the same missing arm.
        state = {
            "doc_type": "contract",
            "extracted_data": {"_unsupported": True},
            "extraction_confidence": 0.3,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "human_review"

    def test_after_extraction_retired_class_skips_retry(self):
        state = {
            "doc_type": "court_opinion",
            "extracted_data": {"caption": "Smith v Jones"},
            "extraction_confidence": 0.95,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "human_review"

    def test_after_extraction_unknown_token_skips_retry(self):
        state = {
            "doc_type": "unknown",
            "extracted_data": {"_unsupported": True},
            "extraction_confidence": 0.3,
            "extraction_attempts": 1,
            "conflict_detected": False,
        }
        assert after_extraction(state) == "human_review"

    def test_after_boss_approved_routes_to_report(self):
        state = {"review_decision": "approved"}
        assert after_boss(state) == "compile_report"

    def test_after_boss_review_routes_to_human(self):
        state = {"review_decision": "review"}
        assert after_boss(state) == "human_review"

    def test_after_boss_transient_retries_not_leftover_approved(self):
        # Review-resume sets review_decision="approved". A Boss blip must NOT
        # treat that leftover flag as a successful adjudication.
        state = {
            "review_decision": "approved",
            "transient_error": True,
            "transient_retries_boss_escalation": 1,
        }
        assert after_boss(state) == "boss_escalation"
        exhausted = {**state, "transient_retries_boss_escalation": 3}
        assert after_boss(exhausted) == "human_review"

    def test_after_retry_classify_transient_self_loops(self):
        looping = {"transient_error": True, "transient_retries_retry_classify": 1}
        assert after_retry_classify(looping) == "retry_classify"
        exhausted = {"transient_error": True, "transient_retries_retry_classify": 3}
        assert after_retry_classify(exhausted) == "human_review"

    def test_after_retry_extraction_transient_self_loops(self):
        looping = {"transient_error": True, "transient_retries_retry_extract": 1}
        assert after_retry_extraction(looping) == "retry_extract"
        exhausted = {"transient_error": True, "transient_retries_retry_extract": 3}
        assert after_retry_extraction(exhausted) == "human_review"

    def test_after_arbiter_transient_self_loops(self):
        looping = {"transient_error": True, "transient_retries_arbiter": 1}
        assert after_arbiter(looping) == "arbiter"
        exhausted = {"transient_error": True, "transient_retries_arbiter": 3}
        assert after_arbiter(exhausted) == "human_review"

    def test_after_retry_classify_unknown_type_before_lane_a(self):
        state = {"classification_confidence": 0.80, "doc_type": "zzz_unknown"}
        assert after_retry_classify(state) == "human_review"

    def test_after_retry_classify_unknown_token_high_confidence_parks(self):
        state = {
            "classification_confidence": 0.99,
            "doc_type": "unknown",
        }
        assert after_retry_classify(state) == "human_review"

    def test_after_review_classify_unknown_token_escalates(self):
        state = {
            "review_verdict": "reviewer_overrides",
            "reviewer_confidence": 0.99,
            "reviewer_doc_type": "unknown",
        }
        assert after_review_classify(state) == "human_review"

    def test_after_human_review_approved(self):
        state = {"review_decision": "approved"}
        assert after_human_review(state) == "compile_report"

    def test_after_human_review_rejected(self):
        state = {"review_decision": "rejected"}
        assert after_human_review(state) == "failed"


class TestTransientPerNodeBudget:
    """L-13: transient retries are budgeted per node — classify failures must
    not exhaust the counter that extract later needs."""

    def test_classify_and_extract_have_independent_budgets(self):
        from graph.routing import after_classify, after_extraction, _transient_decision

        # 2 classify transient failures: still retrying classify.
        assert _transient_decision({"transient_retries_classify": 2}, retry_target="classify") == "retry"
        # 3rd classify failure: review.
        assert _transient_decision({"transient_retries_classify": 3}, retry_target="classify") == "human_review"
        # But extract's budget is untouched — first extract failure still retries.
        assert _transient_decision({"transient_retries_classify": 3}, retry_target="extract") == "retry"
        assert _transient_decision({"transient_retries_extract": 3}, retry_target="extract") == "human_review"
