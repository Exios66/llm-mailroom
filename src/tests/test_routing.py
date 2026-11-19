import pytest

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
    after_retry_extraction_gated,
)
from pipeline.reconsideration import collect_review_causes


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


class TestUnsureExtractionReachesTheJudge:
    """Retries spent on a low or missing confidence: judge first, then a person."""

    def _state(self, **overrides):
        state = {"doc_type": "insurance_claim", "extraction_confidence": None, "extraction_attempts": 3,
                 "extracted_data": {"claim_number": "CLM-1"}}
        return {**state, **overrides}

    def test_missing_confidence_goes_to_the_judge(self):
        assert after_extraction(self._state()) == "judge_verify"

    def test_low_confidence_goes_to_the_judge(self):
        assert after_extraction(self._state(extraction_confidence=0.2)) == "judge_verify"

    def test_a_guardrail_failure_stays_with_a_person(self):
        assert after_extraction(self._state(extraction_guardrail=["unsupported"])) == "human_review"

    def test_no_judge_lane_stays_with_a_person(self, monkeypatch):
        monkeypatch.setenv("MAILROOM_JUDGE_VERIFY", "off")
        assert after_extraction(self._state()) == "human_review"


class TestOnlyConfidenceClearsAnUnsureExtraction:
    """Completeness is not accuracy: the judge and arbiter can repair, not archive."""

    def test_a_complete_verdict_sends_an_unsure_extraction_to_a_person(self):
        for confidence in (None, 0.2):
            state = {"doc_type": "contract", "extraction_confidence": confidence, "judge_verdict": "complete"}
            assert after_judge(state) == "human_review"

    def test_an_accepted_unsure_extraction_goes_to_a_person(self):
        state = {"doc_type": "contract", "extraction_confidence": None, "arbiter_decision": "accept_with_caveats"}
        assert after_arbiter(state) == "human_review"

    def test_a_judge_that_verified_the_whole_source_no_longer_clears_it(self):
        state = {"doc_type": "contract", "extraction_confidence": None, "judge_verdict": "complete",
                 "judge_source_verified": True}
        assert after_judge(state) == "human_review"

    def test_judge_clears_low_confidence_restores_the_old_route(self, approvals_archive):
        state = {"doc_type": "contract", "extraction_confidence": None, "judge_verdict": "complete",
                 "judge_source_verified": True}
        assert after_judge(state) == "compile_report"

    def test_the_arbiter_can_still_send_it_back_for_repair(self):
        state = {"doc_type": "contract", "extraction_confidence": None, "arbiter_decision": "retry_extraction",
                 "arbiter_retry_count": 1}
        assert after_arbiter(state) == "retry_extract"

    def test_a_confident_extraction_passes_only_with_the_old_route(self, approvals_archive):
        state = {"doc_type": "contract", "extraction_confidence": 0.97, "judge_verdict": "complete",
                 "arbiter_decision": "accept_with_caveats"}
        assert after_judge(state) == "compile_report"
        assert after_arbiter(state) == "compile_report"


class TestArchiveChecks:
    """A judge, arbiter or boss approval is not evidence enough to archive on its own (defaults)."""

    def test_a_judge_complete_goes_to_a_person(self):
        state = {"doc_type": "contract", "extraction_confidence": 0.97, "judge_verdict": "complete"}
        assert after_judge(state) == "human_review"
        assert "judge_complete_unconfirmed" in collect_review_causes(state)

    def test_a_skipped_judge_still_follows_confidence(self):
        for verdict in ("skipped", None, ""):
            assert after_judge({"doc_type": "contract", "extraction_confidence": 0.97,
                                "judge_verdict": verdict}) == "compile_report"

    def test_an_accept_with_caveats_goes_to_a_person(self):
        state = {"doc_type": "contract", "extraction_confidence": 0.97, "judge_verdict": "partial",
                 "arbiter_decision": "accept_with_caveats"}
        assert after_arbiter(state) == "human_review"
        assert "arbiter_accept_unconfirmed" in collect_review_causes(state)

    _RETRY = {"doc_type": "contract", "extraction_confidence": 0.99, "extraction_attempts": 2,
              "judge_pass_count": 1, "arbiter_decision": "retry_extraction", "arbiter_retry_count": 1,
              "gate_ok": True, "schema_valid": True}

    def test_a_retry_the_judge_doubted_never_archives_unjudged(self):
        """#241 hole: the arbiter orders a retry after incomplete/partial and the retry says 0.99."""
        for verdict in ("incomplete", "partial"):
            state = {**self._RETRY, "judge_verdict": verdict}
            assert after_retry_extraction_gated(state) == "human_review"
            assert "retry_unjudged" in collect_review_causes(state)

    def test_the_old_route_is_one_flag_away(self, approvals_archive):
        assert after_retry_extraction_gated({**self._RETRY, "judge_verdict": "incomplete"}) == "compile_report"

    @pytest.mark.parametrize("extra", [
        {"arbiter_retry_count": 3},                                 # arbiter past its cap: no retry ran
        {"arbiter_retry_count": 0},                                 # a review resume resets the count
        {"extraction_attempts": 1},                                 # no retry yet
        {"conflict_detected": True, "review_decision": None},       # parked for the matter conflict
        {"extraction_confidence": 0.2, "extraction_attempts": 3},   # parked for low confidence
    ])
    def test_retry_unjudged_names_only_a_retry_that_would_have_archived(self, extra):
        state = {**self._RETRY, "judge_verdict": "incomplete", **extra}
        assert "retry_unjudged" not in collect_review_causes(state)

    def test_a_retry_the_judge_judged_again_is_not_unjudged(self):
        """The latest attempt carries its own verdict: a re-judged partial is not retry_unjudged."""
        judged = {**self._RETRY, "judge_verdict": "partial", "extraction_attempts": 3, "arbiter_retry_count": 2,
                  "extraction_attempt_history": [{"attempt_no": n, "judge_verdict": "partial"} for n in (1, 2, 3)]}
        assert "retry_unjudged" not in collect_review_causes(judged)
        unjudged = {**judged, "extraction_attempt_history": [
            {"attempt_no": 1, "judge_verdict": "partial"}, {"attempt_no": 2, "judge_verdict": "partial"},
            {"attempt_no": 3, "judge_verdict": None}]}
        assert "retry_unjudged" in collect_review_causes(unjudged)
        assert after_retry_extraction_gated(unjudged) == "human_review"

    def test_a_boss_approval_does_not_judge_a_doubted_retry(self):
        """A doubted retry that finds a matter conflict goes to the Boss, not the judge."""
        from graph.routing import after_boss

        state = {**self._RETRY, "judge_verdict": "incomplete", "conflict_detected": True,
                 "review_decision": "approved", "boss_context_records": 2}
        assert after_boss(state) == "human_review"
        assert "retry_unjudged" in collect_review_causes(state)
        assert after_boss({**state, "arbiter_retry_count": 0}) == "compile_report"  # a review resume

    def test_a_boss_approval_of_a_doubted_retry_archives_when_opted_back_in(self, approvals_archive):
        from graph.routing import after_boss

        state = {**self._RETRY, "judge_verdict": "incomplete", "conflict_detected": True,
                 "review_decision": "approved", "boss_context_records": 2}
        assert after_boss(state) == "compile_report"

    def test_a_retry_with_no_judge_doubt_still_archives(self):
        state = {"doc_type": "contract", "extraction_confidence": 0.99, "extraction_attempts": 2,
                 "gate_ok": True, "schema_valid": True}
        assert after_retry_extraction_gated(state) == "compile_report"

    def test_a_class_can_opt_back_in(self, monkeypatch):
        import copy

        import pipeline.config as config

        cfg = copy.deepcopy(config.load_config())
        cfg["confidence"]["by_class"]["insurance_claim"]["judge_complete_archives"] = True
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        claim = {"doc_type": "insurance_claim", "extraction_confidence": 0.97, "judge_verdict": "complete"}
        assert after_judge(claim) == "compile_report"
        assert after_judge({**claim, "doc_type": "contract"}) == "human_review"

    def test_a_blind_boss_approval_goes_to_a_person(self):
        from graph.routing import after_boss

        state = {"doc_type": "contract", "review_decision": "approved", "conflict_detected": True,
                 "boss_context_records": 0}
        assert after_boss(state) == "human_review"
        assert "boss_ungrounded" in collect_review_causes(state)
        assert after_boss({**state, "boss_context_records": 2}) == "compile_report"
        legacy = {k: v for k, v in state.items() if k != "boss_context_records"}
        assert after_boss(legacy) == "compile_report"  # a state from before the count was kept


def test_every_specialist_schema_asks_for_confidence():
    from langchain_agents.specialist_agents import SPECIALIST_SCHEMAS

    for doc_type, schema in SPECIALIST_SCHEMAS.items():
        assert "confidence" in schema["properties"], doc_type
        assert "confidence" in schema["required"], doc_type


class _Judge:
    def __init__(self, accuracy):
        self.accuracy = accuracy
        self.checked = 0

    def judge_completeness(self, **_):
        return {"completeness": 1.0, "completeness_label": "complete", "reasoning": ""}

    def judge_extraction_correctness(self, **_):
        self.checked += 1
        return {"extraction_correctness": 0.4, "extraction_correctness_label": self.accuracy, "reasoning": "wrong date"}


def _judged(monkeypatch, accuracy, text="short source"):
    import agents.judge
    import graph.build_graph as bg

    fake = _Judge(accuracy)
    monkeypatch.setattr(agents.judge, "CompletenessJudge", lambda: fake)
    monkeypatch.setenv("MAILROOM_JUDGE_VERIFY", "on")
    return bg.judge_verify_node({"doc_type": "contract", "extraction_confidence": None, "doc_text": text,
                                 "extracted_data": {"parties": ["A"]}}), fake


def test_a_complete_but_inaccurate_extraction_is_not_complete(monkeypatch):
    result, fake = _judged(monkeypatch, "inaccurate")
    assert fake.checked == 1
    assert result["judge_verdict"] == "incomplete" and result["judge_source_verified"] is False
    assert "wrong date" in result["judge_findings"]


def test_a_judge_that_reports_no_coverage_verifies_nothing(monkeypatch):
    """The fake judge reads no windows, so even a clean verdict does not clear a long document."""
    result, _ = _judged(monkeypatch, "accurate", text="x" * 50_000)
    assert result["judge_verdict"] == "complete" and result["judge_source_verified"] is False
    assert result["judge_coverage"]["windows_checked"] == 0


def test_the_arbiter_is_shown_the_source(monkeypatch):
    from agents.arbiter import ArbiterAgent

    sent = {}
    monkeypatch.setattr(ArbiterAgent, "_call_structured",
                        lambda self, message, schema: sent.update(message=message) or {"decision": "human_review"})
    ArbiterAgent().arbitrate("contract", {}, "partial", [], doc_text="the source text")
    assert "the source text" in sent["message"]
