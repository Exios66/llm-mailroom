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
            "classification_confidence": 0.98,
            "classification_attempts": 1,
            "doc_type": "contract",
        }
        assert after_classify(state) == "extract"

    def test_after_classify_medium_confidence_routes_to_review(self):
        # Medium band (low <= confidence < high): classified but not clearly
        # confident. First passes get re-classification; past retry_max → review.
        state = {
            "classification_confidence": 0.90,
            "classification_attempts": 1,
            "doc_type": "correspondence",
        }
        assert after_classify(state) == "retry_classify"
        exhausted = {**state, "classification_attempts": 3}
        assert after_classify(exhausted) == "human_review"

    def test_after_classify_medium_confidence_exhausts_no_retry_budget(self):
        # Contract severity: medium band is [0.90, 0.98).
        state = {
            "classification_confidence": 0.93,
            "classification_attempts": 0,
            "doc_type": "contract",
        }
        assert after_classify(state) == "retry_classify"
        assert after_classify({**state, "classification_attempts": 3}) == "human_review"

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
            "classification_attempts": 3,
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
        # merger_agreement is a live MAUD class, not an alias of CUAD contract.
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
            "extraction_attempts": 3,
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
            "extraction_attempts": 3,
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
        assert after_human_review(state) == "extract"

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


class TestDocumentStateSchema:
    """LangGraph drops any key a node returns that DocumentState does not
    declare, so every key a node reads back must be in the schema."""

    def test_keys_read_by_nodes_are_declared(self):
        import re
        from pathlib import Path

        from graph.state import DocumentState

        src = (Path(__file__).resolve().parents[1] / "graph" / "build_graph.py").read_text()
        read = set(re.findall(r'state\.get\(\s*"(\w+)"', src))
        # run_id is passed as an argument; state.get("run_id") is only a fallback.
        undeclared = read - set(DocumentState.__annotations__) - {"run_id"}
        assert not undeclared, f"undeclared state keys: {sorted(undeclared)}"

    def test_transient_retry_counter_survives_real_state_schema(self):
        from langgraph.graph import END, START, StateGraph

        from graph.state import DocumentState

        calls = []

        def classify(state):
            n = state.get("transient_retries_classify", 0) + 1
            calls.append(n)
            return {"transient_error": True, "transient_retries_classify": n}

        g = StateGraph(DocumentState)
        g.add_node("classify", classify)
        g.add_node("human_review", lambda s: {"stage": "review"})
        g.add_edge(START, "classify")
        g.add_conditional_edges(
            "classify",
            after_classify,
            {"classify": "classify", "human_review": "human_review", "extract": END,
             "retry_classify": END, "review_classify": END, "boss_escalation": END},
        )
        g.add_edge("human_review", END)
        out = g.compile().invoke({"doc_id": "x"}, {"recursion_limit": 20})
        assert calls == [1, 2, 3]
        assert out["stage"] == "review"


class TestLaneBZeroBudgets:
    """A configured budget of 0 must be honoured, not replaced by the default
    through ``or``-coalescing (0 is falsy)."""

    @staticmethod
    def _budgets(monkeypatch, **overrides):
        import graph.routing as routing

        monkeypatch.setattr(routing, "_thresholds_for", lambda state: dict(overrides))

    def test_zero_arbiter_retry_max_disables_arbiter_retries(self, monkeypatch):
        self._budgets(monkeypatch, arbiter_retry_max=0)
        state = {"arbiter_decision": "retry_extraction", "arbiter_retry_count": 1}
        assert after_arbiter(state) == "human_review"

    def test_zero_judge_max_passes_escalates_first_failed_verdict(self, monkeypatch):
        self._budgets(monkeypatch, judge_max_passes=0)
        state = {"judge_verdict": "partial", "judge_pass_count": 1}
        assert after_judge(state) == "human_review"

    def test_missing_budgets_keep_defaults(self, monkeypatch):
        self._budgets(monkeypatch)
        assert after_arbiter({"arbiter_decision": "retry_extraction", "arbiter_retry_count": 2}) == "retry_extract"
        assert after_judge({"judge_verdict": "partial", "judge_pass_count": 2}) == "arbiter"
        assert after_judge({"judge_verdict": "partial", "judge_pass_count": 3}) == "human_review"
