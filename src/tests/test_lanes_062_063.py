"""KANBAN-062/063 — Lane A (Sorter Review) + Lane B (Judge/Arbiter) tests.

Network-free: routers are pure functions; node tests stub the LLM agents at
their module boundaries. Mirrors the house style of test_routing.py.
"""

import pytest

from graph.routing import (
    after_arbiter,
    after_extraction_gated,
    after_judge,
    after_review_classify,
    after_retry_classify,
    judge_gate,
)


class TestLaneARouting:
    def test_medium_band_after_retry_goes_to_agent_review(self):
        # KANBAN-062: a medium-band classification that survived the
        # re-classification pass now gets an agent second opinion BEFORE any
        # human sees it.
        state = {"classification_confidence": 0.80, "doc_type": "contract"}
        assert after_retry_classify(state) == "review_classify"

    def test_high_confidence_post_retry_skips_lane(self):
        # Retry resolved the ambiguity upward -> straight to extraction, lane
        # untouched (hot-path behavior preserved).
        state = {"classification_confidence": 0.97, "doc_type": "contract"}
        assert after_retry_classify(state) == "extract"

    def test_low_confidence_post_retry_still_human_review(self):
        state = {"classification_confidence": 0.40, "doc_type": "contract"}
        assert after_retry_classify(state) == "human_review"

    def test_unknown_type_post_retry_still_human_review(self):
        state = {"classification_confidence": 0.97, "doc_type": "zzz_unknown"}
        assert after_retry_classify(state) == "human_review"

    def test_transient_error_self_loops_on_own_budget(self):
        # L-13 per-node budgets: retry_classify transients must retry the SAME
        # node (the re-evaluation prompt), not bounce back to first-pass classify.
        state = {
            "transient_error": True,
            "transient_retries_retry_classify": 1,
            "classification_confidence": 0.80,
        }
        assert after_retry_classify(state) == "retry_classify"
        exhausted = {
            "transient_error": True,
            "transient_retries_retry_classify": 3,
            "classification_confidence": 0.80,
        }
        assert after_retry_classify(exhausted) == "human_review"

    def test_review_verdict_winning_routes_to_extract(self):
        assert (
            after_review_classify({
                "review_verdict": "reviewer_agrees_high",
                "reviewer_confidence": 0.97,
                "reviewer_doc_type": "contract",
            })
            == "extract"
        )
        assert (
            after_review_classify({
                "review_verdict": "reviewer_overrides",
                "reviewer_confidence": 0.98,
                "reviewer_doc_type": "court_opinion",
            })
            == "extract"
        )

    def test_review_verdict_unsure_escalates(self):
        base = {"reviewer_doc_type": "contract"}
        for verdict, conf in [
            ("reviewer_agrees_low", 0.85),
            ("reviewer_conflicts", 0.90),
            ("reviewer_error", 0.97),
        ]:
            assert after_review_classify({**base, "review_verdict": verdict, "reviewer_confidence": conf}) == "human_review"

    def test_review_override_with_invalid_type_escalates(self):
        assert (
            after_review_classify({
                "review_verdict": "reviewer_overrides",
                "reviewer_confidence": 0.99,
                "reviewer_doc_type": "made_up_class",
            })
            == "human_review"
        )

    def test_review_transient_self_loop_then_exhaustion(self):
        looping = {"transient_error": True, "transient_retries_review_classify": 1}
        assert after_review_classify(looping) == "review_classify"
        exhausted = {"transient_error": True, "transient_retries_review_classify": 3}
        assert after_review_classify(exhausted) == "human_review"


class TestJudgeGate:
    def test_clean_run_never_enters_judge(self):
        # THE cost contract: high-confidence extractions keep today's path
        # with zero added LLM calls.
        assert judge_gate({"extraction_confidence": 0.99}) is False
        assert after_extraction_gated({"extraction_confidence": 0.99, "extraction_attempts": 1}) == "compile_report"

    def test_ambiguous_band_detours_to_judge(self):
        assert judge_gate({"extraction_confidence": 0.75}) is True
        assert after_extraction_gated({"extraction_confidence": 0.75, "extraction_attempts": 1}) == "judge_verify"

    def test_band_edges_exclusive(self):
        assert judge_gate({"extraction_confidence": 0.70}) is True   # low edge inclusive
        assert judge_gate({"extraction_confidence": 0.85}) is False  # band top exclusive
        assert judge_gate({"extraction_confidence": 0.69}) is False  # below band = retry territory
        assert judge_gate({"extraction_confidence": None}) is False

    def test_kill_switch_env(self, monkeypatch):
        monkeypatch.setenv("MAILROOM_JUDGE_VERIFY", "off")
        assert judge_gate({"extraction_confidence": 0.75}) is False
        assert after_extraction_gated({"extraction_confidence": 0.75, "extraction_attempts": 1}) == "compile_report"

    def test_conflict_still_beats_gate(self):
        state = {"extraction_confidence": 0.75, "extraction_attempts": 1, "conflict_detected": True}
        assert after_extraction_gated(state) == "boss_escalation"

    def test_gated_router_matches_plain_router_everywhere_else(self):
        low = {"extraction_confidence": 0.30, "extraction_attempts": 3}
        assert after_extraction_gated(low) == "human_review"
        schema_bad = {
            "extraction_confidence": 0.95,
            "extraction_attempts": 1,
            "extracted_data": {"_bad": True},
            "schema_valid": False,
        }
        # (schema_valid key isn't how the router reads it; validate_extraction
        # runs inside after_extraction — this state simply exercises the
        # passthrough arm.)
        assert after_extraction_gated(schema_bad) in ("retry_extract", "compile_report")


class TestLaneBRouting:
    def test_complete_and_skipped_proceed(self):
        for verdict in ("complete", "skipped", None, ""):
            assert after_judge({"judge_verdict": verdict}) == "compile_report"

    def test_partial_and_incomplete_go_to_arbiter(self):
        for verdict in ("partial", "incomplete"):
            assert after_judge({"judge_verdict": verdict}) == "arbiter"

    def test_judge_hard_failure_fails_safe_to_human(self):
        assert after_judge({"judge_verdict": "judge_error"}) == "human_review"

    def test_judge_transient_self_loop_on_own_budget(self):
        assert after_judge({"transient_error": True, "transient_retries_judge_verify": 1}) == "judge_verify"
        assert after_judge({"transient_error": True, "transient_retries_judge_verify": 3}) == "human_review"

    def test_arbiter_accept_proceeds(self):
        assert after_arbiter({"arbiter_decision": "accept_with_caveats"}) == "compile_report"

    def test_arbiter_retry_bounded_to_one(self):
        # KANBAN-098: the bound is approval-INCLUSIVE. arbiter_node increments
        # arbiter_retry_count when the arbiter ORDERS the retry, so the FIRST
        # approval already arrives at this router carrying count == 1 and must
        # still dispatch to retry_extract. A SECOND arbitration demanding yet
        # another retry finds the budget spent (count == 2) and escalates.
        first_approval = {"arbiter_decision": "retry_extraction", "arbiter_retry_count": 1}
        assert after_arbiter(first_approval) == "retry_extract"
        spent = {"arbiter_decision": "retry_extraction", "arbiter_retry_count": 2}
        assert after_arbiter(spent) == "human_review"

    def test_arbiter_human_review_escalates(self):
        assert after_arbiter({"arbiter_decision": "human_review"}) == "human_review"


class TestTopology:
    def test_all_thirteen_nodes_registered(self):
        from graph.build_graph import build_graph

        g = build_graph()
        nodes = {n for n in g.get_graph().nodes if not n.startswith("__")}
        expected = {
            "ingest", "classify", "retry_classify", "review_classify",
            "extract", "retry_extract", "judge_verify", "arbiter",
            "human_review", "boss_escalation", "compile_report",
            "catalog_write", "archive",
        }
        assert nodes == expected

    def test_lane_edges_exist(self):
        from graph.build_graph import build_graph

        g = build_graph()
        edges = {(e.source, e.target) for e in g.get_graph().edges}
        required = {
            ("__start__", "ingest"),
            ("retry_classify", "review_classify"),
            ("retry_classify", "retry_classify"),
            ("review_classify", "extract"),
            ("review_classify", "human_review"),
            ("extract", "judge_verify"),
            ("retry_extract", "judge_verify"),
            ("retry_extract", "retry_extract"),
            ("judge_verify", "compile_report"),
            ("judge_verify", "arbiter"),
            ("arbiter", "retry_extract"),
            ("arbiter", "human_review"),
            ("arbiter", "compile_report"),
            ("arbiter", "arbiter"),
            ("boss_escalation", "boss_escalation"),
        }
        missing = required - edges
        assert not missing, f"lane edges missing: {missing}"
        # Transient blips on retry nodes must self-loop, not bounce to the
        # first-pass node (that dropped the re-evaluation / arbiter fix-list).
        assert ("retry_extract", "extract") not in edges
        assert ("retry_classify", "classify") not in edges


class TestNodeBehavior:
    @pytest.fixture()
    def fake_reviewer_agree(self, monkeypatch):
        class FakeAgent:
            def __init__(self):
                pass

            def review(self, doc_text, pages=None, **kw):
                return {
                    "doc_type": "contract",
                    "contract_subtype": "other",
                    "confidence": 0.97,
                    "reasoning": "clear contractual form",
                }

        import agents.sorter_reviewer as mod

        monkeypatch.setattr(mod, "SorterReviewerAgent", FakeAgent)

    def test_review_node_applies_winning_override_label(self, monkeypatch):
        class FakeAgent:
            def __init__(self):
                pass

            def review(self, doc_text, pages=None, **kw):
                return {
                    "doc_type": "court_opinion",
                    "contract_subtype": None,
                    "confidence": 0.98,
                    "reasoning": "judicial decision format",
                }

        import agents.sorter_reviewer as mod

        monkeypatch.setattr(mod, "SorterReviewerAgent", FakeAgent)
        from graph.build_graph import review_classify_node

        updates = review_classify_node(
            {"doc_id": "d1", "doc_text": "text", "doc_type": "correspondence",
             "classification_confidence": 0.8}
        )
        assert updates["review_verdict"] == "reviewer_overrides"
        assert updates["doc_type"] == "court_opinion"          # label APPLIED
        assert updates["classification_confidence"] == 0.98
        # Original sorter answer preserved for the audit trail:
        assert updates["reviewer_doc_type"] == "court_opinion"
        assert "sorter='correspondence'" in updates["escalation_reason"]

    def test_review_node_low_confidence_keeps_sorter_label(self, monkeypatch):
        class FakeAgent:
            def __init__(self):
                pass

            def review(self, doc_text, pages=None, **kw):
                return {
                    "doc_type": "contract",
                    "contract_subtype": None,
                    "confidence": 0.60,
                    "reasoning": "uncertain",
                }

        import agents.sorter_reviewer as mod

        monkeypatch.setattr(mod, "SorterReviewerAgent", FakeAgent)
        from graph.build_graph import review_classify_node

        updates = review_classify_node(
            {"doc_id": "d1", "doc_text": "text", "doc_type": "correspondence",
             "classification_confidence": 0.8}
        )
        assert updates["review_verdict"] == "reviewer_conflicts"
        assert "doc_type" not in updates                       # sorter label UNTOUCHED
        assert updates["escalation_reason"]                    # both opinions surfaced

    def test_review_node_hard_failure_preserves_answer_and_escalates(self, monkeypatch):
        class BoomAgent:
            def __init__(self):
                raise RuntimeError("config exploded")

        import agents.sorter_reviewer as mod

        monkeypatch.setattr(mod, "SorterReviewerAgent", BoomAgent)
        from graph.build_graph import review_classify_node

        updates = review_classify_node(
            {"doc_id": "d1", "doc_text": "t", "doc_type": "contract",
             "classification_confidence": 0.8}
        )
        assert updates["review_verdict"] == "reviewer_error"
        assert "human review" in updates["escalation_reason"]
        assert "doc_type" not in updates

    def test_judge_node_skips_without_llm_call_when_gated_out(self, monkeypatch):
        import agents.judge as jmod

        def _boom(*a, **k):
            raise AssertionError("CompletenessJudge constructed despite clean run")

        monkeypatch.setattr(jmod, "CompletenessJudge", _boom)
        from graph.build_graph import judge_verify_node

        result = judge_verify_node({"doc_id": "d1", "extraction_confidence": 0.99})
        assert result["judge_verdict"] == "skipped"
        assert result["transient_error"] is False

    def test_judge_node_scores_dirty_fields_only(self, monkeypatch):
        seen = {}

        class FakeJudge:
            def __init__(self):
                pass

            def judge_completeness(self, doc_type, extracted, doc_text):
                seen.update(doc_type=doc_type, extracted=extracted, doc_text=doc_text)
                return {"completeness": 0.4, "completeness_label": "incomplete", "reasoning": "missing fields X, Y"}

        import agents.judge as jmod

        monkeypatch.setattr(jmod, "CompletenessJudge", FakeJudge)
        from graph.build_graph import judge_verify_node

        result = judge_verify_node({
            "doc_id": "d1",
            "doc_type": "contract",
            "doc_text": "src",
            "extraction_confidence": 0.75,
            "extracted_data": {"parties": "A/B", "_trace_id": "xyz", "reasoning": "chain-of-thought"},
        })
        assert result["judge_verdict"] == "incomplete"
        assert seen["extracted"] == {"parties": "A/B"}         # metadata stripped
        assert seen["doc_text"] == "src"

    def test_arbiter_node_counts_its_retry(self, monkeypatch):
        calls = {}

        class FakeArbiter:
            def __init__(self):
                pass

            def arbitrate(self, **kw):
                calls.update(kw)
                return {
                    "decision": "retry_extraction",
                    "fields_to_fix": ["effective_date"],
                    "reasoning": "recoverable miss",
                    "handoff_summary": "re-extract with effective_date",
                }

        import agents.arbiter as amod

        monkeypatch.setattr(amod, "ArbiterAgent", FakeArbiter)
        from graph.build_graph import arbiter_node

        updates = arbiter_node({
            "doc_id": "d1",
            "doc_type": "contract",
            "extracted_data": {"parties": "A"},
            "judge_verdict": "incomplete",
            "judge_findings": ["missing effective_date"],
            "judge_score": 0.4,
            "arbiter_retry_count": 0,
        })
        assert updates["arbiter_decision"] == "retry_extraction"
        assert updates["arbiter_retry_count"] == 1             # bound counter moved
        assert updates["arbiter_fields_to_fix"] == ["effective_date"]
        assert updates["transient_error"] is False
        assert calls["judge_score"] == 0.4
        assert "_trace" not in str(calls["extracted"])

    def test_arbiter_node_failure_fails_safe(self, monkeypatch):
        class BoomArbiter:
            def __init__(self):
                raise RuntimeError("no provider")

        import agents.arbiter as amod

        monkeypatch.setattr(amod, "ArbiterAgent", BoomArbiter)
        from graph.build_graph import arbiter_node

        updates = arbiter_node({"doc_id": "d1"})
        assert updates["arbiter_decision"] == "human_review"
        assert "arbitration failed" in updates["escalation_reason"]

    def test_clean_fields_helper(self):
        from graph.build_graph import _clean_fields_for_judge

        dirty = {"a": 1, "_meta": 2, "reasoning": "cot", "__dunder__": 3}
        assert _clean_fields_for_judge(dirty) == {"a": 1}

    def test_handoff_context_includes_fix_list_on_arbiter_retry(self):
        from graph.build_graph import _build_handoff_context

        plain = _build_handoff_context(
            {"doc_type": "contract", "contract_subtype": "nda", "classification_confidence": 0.9}
        )
        assert plain is not None
        assert "ARBITER RETRY" not in plain
        retry = _build_handoff_context(
            {"doc_type": "contract", "arbiter_retry_count": 1,
             "judge_findings": ["missing effective_date"],
             "arbiter_fields_to_fix": ["term_length"],
             "arbiter_handoff": "put the term in ISO form"}
        )
        assert retry is not None
        assert "ARBITER RETRY" in retry
        assert "effective_date" in retry
        assert "term_length" in retry
        assert "ISO form" in retry
