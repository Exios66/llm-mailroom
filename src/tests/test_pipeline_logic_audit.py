"""Regression pins for operational / logic bugs found in the pipeline audit.

Network-free. Each test names the composed-path failure it would have allowed:
sticky transient_error loops, retry nodes writing the wrong counter, leftover
review_decision="approved" skipping Boss adjudication, reporter crashes after
a successful extract, and hard failures in retry_classify crashing to failed.
"""

import openai

from graph import build_graph as bg
from graph.routing import after_boss, after_judge, after_retry_classify, after_retry_extraction


class TestStickyTransientErrorClearedOnSuccess:
    """LangGraph merges partial updates: if a success path omits
    transient_error=False, a previous True sticks and the router self-loops
    forever (counter never increments on success)."""

    def test_judge_success_clears_flag_so_router_does_not_loop(self, monkeypatch):
        class FakeJudge:
            def __init__(self):
                pass

            def judge_completeness(self, **kw):
                return {
                    "completeness": 0.99,
                    "completeness_label": "complete",
                    "reasoning": "ok",
                }

        monkeypatch.setattr("agents.judge.CompletenessJudge", FakeJudge)
        prior = {
            "doc_id": "d1",
            "doc_type": "contract",
            "doc_text": "src",
            "extraction_confidence": 0.75,
            "extracted_data": {"parties": ["A"]},
            "transient_error": True,
            "transient_retries_judge_verify": 1,
        }
        updates = bg.judge_verify_node(prior)
        assert updates["judge_verdict"] == "complete"
        assert updates["transient_error"] is False
        merged = {**prior, **updates}
        assert after_judge(merged) == "compile_report"

    def test_judge_hard_fail_clears_flag_so_router_escalates(self, monkeypatch):
        class BoomJudge:
            def __init__(self):
                raise RuntimeError("config exploded")

        monkeypatch.setattr("agents.judge.CompletenessJudge", BoomJudge)
        prior = {
            "doc_id": "d1",
            "doc_type": "contract",
            "doc_text": "src",
            "extraction_confidence": 0.75,
            "extracted_data": {"parties": ["A"]},
            "transient_error": True,
            "transient_retries_judge_verify": 1,
        }
        updates = bg.judge_verify_node(prior)
        assert updates["judge_verdict"] == "judge_error"
        assert updates["transient_error"] is False
        merged = {**prior, **updates}
        assert after_judge(merged) == "human_review"

    def test_review_classify_hard_fail_clears_flag(self, monkeypatch):
        class Boom:
            def __init__(self):
                raise RuntimeError("nope")

        monkeypatch.setattr("agents.sorter_reviewer.SorterReviewerAgent", Boom)
        prior = {
            "doc_id": "d1",
            "doc_text": "t",
            "doc_type": "contract",
            "classification_confidence": 0.8,
            "transient_error": True,
            "transient_retries_review_classify": 1,
        }
        updates = bg.review_classify_node(prior)
        assert updates["review_verdict"] == "reviewer_error"
        assert updates["transient_error"] is False


class TestRetryNodeTransientCounters:
    def test_retry_classify_writes_own_counter_and_router_self_loops(self, monkeypatch):
        monkeypatch.setattr(
            "agents.sorter.SorterAgent.classify_json",
            lambda *a, **k: (_ for _ in ()).throw(openai.APIConnectionError(request=None)),
        )
        updates = bg.retry_classify_node(
            {"doc_id": "d1", "doc_text": "text", "classification_attempts": 1, "doc_type": "contract"}
        )
        assert updates["transient_error"] is True
        assert updates["transient_retries_retry_classify"] == 1
        assert "transient_retries" not in updates or updates.get("transient_retries") != 1
        merged = {"classification_attempts": 1, **updates}
        assert after_retry_classify(merged) == "retry_classify"

    def test_retry_extract_writes_own_counter_and_router_self_loops(self, monkeypatch):
        def _boom(*a, **k):
            raise openai.APIConnectionError(request=None)

        monkeypatch.setattr(bg, "_build_specialist_dispatch", lambda: {"contract": _boom})
        updates = bg.retry_extract_node(
            {
                "doc_id": "d1",
                "doc_type": "contract",
                "doc_text": "text",
                "extracted_data": {"parties": ["A"]},
                "extraction_attempts": 1,
            }
        )
        assert updates["transient_error"] is True
        assert updates["transient_retries_retry_extract"] == 1
        merged = {"extraction_attempts": 1, **updates}
        assert after_retry_extraction(merged) == "retry_extract"

    def test_retry_classify_hard_fail_routes_to_review_not_crash(self, monkeypatch):
        monkeypatch.setattr(
            "agents.sorter.SorterAgent.classify_json",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        updates = bg.retry_classify_node(
            {"doc_id": "d1", "doc_text": "text", "classification_attempts": 1, "doc_type": "contract"}
        )
        from pipeline.config import get_confidence_thresholds

        retry_max = get_confidence_thresholds().get("retry_max", 1)
        assert updates["classification_attempts"] > retry_max
        assert updates["transient_error"] is False
        assert "human review" in updates["escalation_reason"]
        merged = {"doc_type": "contract", **updates}
        assert after_retry_classify(merged) == "human_review"


class TestBossLeftoverApprovedDoesNotSkipAdjudication:
    def test_resume_approved_plus_boss_blip_retries_boss(self):
        state = {
            "review_decision": "approved",  # leftover from resume_from_review
            "transient_error": True,
            "transient_retries_boss_escalation": 1,
        }
        assert after_boss(state) == "boss_escalation"


class TestCompileReportFailSafe:
    def test_none_extracted_data_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(
            "agents.reporter.compile_matter_record",
            lambda *a, **k: {"summary": "ok"},
        )
        monkeypatch.setattr("llm.client.get_llm", lambda name: (object(), "test-model"))
        result = bg.compile_report_node(
            {
                "doc_id": "d1",
                "doc_type": "contract",
                "extracted_data": None,
                "classification_confidence": 0.9,
                "extraction_confidence": 0.9,
            }
        )
        assert "_report" in result["extracted_data"]
        assert result["extracted_data"]["_report"]["summary"] == "ok"

    def test_reporter_exception_archives_with_fallback(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("reporter down")

        monkeypatch.setattr("agents.reporter.compile_matter_record", _boom)
        monkeypatch.setattr("llm.client.get_llm", lambda name: (object(), "test-model"))
        result = bg.compile_report_node(
            {
                "doc_id": "d1",
                "doc_type": "correspondence",
                "extracted_data": {"sender": "A", "recipient": "B"},
                "classification_confidence": 0.9,
                "extraction_confidence": 0.9,
            }
        )
        report = result["extracted_data"]["_report"]
        assert report.get("error") is True
        assert result["report_error"] is True
        assert "reporter down" in report["summary"] or "RuntimeError" in report["summary"]
        assert result["extracted_data"]["sender"] == "A"
        from graph.routing import after_report

        merged = {
            "doc_id": "d1",
            "doc_type": "correspondence",
            **result,
        }
        assert after_report(merged) == "human_review"


class TestClassificationGuardClampsInvalidSubtype:
    def test_high_confidence_contract_missing_subtype_does_not_auto_extract(self, monkeypatch):
        monkeypatch.setattr(
            "agents.sorter.SorterAgent.classify_json",
            lambda *a, **k: {
                "doc_type": "contract",
                "contract_subtype": None,
                "doc_subclass": None,
                "confidence": 0.97,
                "reasoning": "looks like a contract",
            },
        )
        updates = bg.classify_node(
            {"doc_id": "d1", "doc_text": "agreement", "classification_attempts": 0}
        )
        assert updates["classification_confidence"] == 0.5
        assert updates["classification_guardrail"]
        from graph.routing import after_classify

        # 0.5 is below low → retry, not extract
        assert after_classify({**updates, "classification_attempts": 1}) == "retry_classify"


class TestReviewerReceivesSubtypeVocabulary:
    def test_review_passes_cuad_subtype_list(self, monkeypatch):
        seen = {}

        class FakeAgent:
            def __init__(self):
                pass

            def review(self, doc_text, pages=None, valid_doc_types=None, contract_subtypes=None):
                seen["subtypes"] = contract_subtypes
                seen["types"] = valid_doc_types
                return {
                    "doc_type": "insurance_claim",
                    "contract_subtype": None,
                    "doc_subclass": "carrier",
                    "confidence": 0.98,
                    "reasoning": "judicial form",
                }

        monkeypatch.setattr("agents.sorter_reviewer.SorterReviewerAgent", FakeAgent)
        bg.review_classify_node(
            {
                "doc_id": "d1",
                "doc_text": "text",
                "doc_type": "correspondence",
                "classification_confidence": 0.8,
            }
        )
        assert seen["subtypes"]
        assert "other" in seen["subtypes"]
        assert seen["types"]
        assert "contract" in seen["types"]
        assert "unknown" in seen["types"]


class TestSpecialistMemoryName:
    def test_maps_doc_type_to_configured_specialist(self):
        assert bg._specialist_memory_name("insurance_claim") == "insurance_claims_specialist"
        assert bg._specialist_memory_name("contract") == "contracts_specialist"
        assert bg._specialist_memory_name("merger_agreement") == "contracts_specialist"

    def test_unmapped_type_does_not_fall_back_to_contracts(self):
        assert bg._specialist_memory_name("court_opinion") is None
        assert bg._specialist_memory_name("unknown") is None
        assert bg._specialist_memory_name("not_a_class") is None


class TestUnsupportedExtractParksWithoutRetry:
    def test_retired_class_extract_is_unsupported_stub(self):
        from graph.routing import after_extraction

        prior = {
            "doc_id": "d1",
            "doc_type": "court_opinion",
            "doc_text": "IN THE SUPREME COURT",
            "extraction_attempts": 0,
        }
        updates = bg.extract_node(prior)
        assert updates["extracted_data"]["_unsupported"] is True
        assert updates["extraction_confidence"] == 0.0
        merged = {**prior, **updates}
        assert after_extraction(merged) == "human_review"

    def test_merger_agreement_dispatches_contracts_specialist(self, monkeypatch):
        from graph.routing import after_extraction

        called = {}

        def fake_contracts(doc_text, pages=None, handoff_context=None):
            called["text"] = doc_text
            called["handoff"] = handoff_context
            return {
                "parties": ["Parent Inc.", "Target Corp."],
                "document_name": "Agreement and Plan of Merger",
                "confidence": 0.92,
            }

        monkeypatch.setattr(bg, "_extract_contracts", fake_contracts)
        monkeypatch.setattr(
            bg,
            "_specialist_extractor_map",
            lambda: {
                "contracts_specialist": fake_contracts,
                "corporate_records_specialist": bg._extract_corporate_records,
                "correspondence_specialist": bg._extract_correspondence,
                "compliance_specialist": bg._extract_compliance,
                "insurance_claims_specialist": bg._extract_insurance_claims,
            },
        )
        prior = {
            "doc_id": "d-ma",
            "doc_type": "merger_agreement",
            "doc_text": "AGREEMENT AND PLAN OF MERGER",
            "extraction_attempts": 0,
        }
        updates = bg.extract_node(prior)
        assert called.get("text")
        handoff = called.get("handoff") or ""
        assert "doc_type=merger_agreement" in handoff
        assert "MAUD extraction" in handoff
        # Shared specialist, distinct class — no extract alias onto contract.
        assert "extract_class=contract" not in handoff
        assert updates["extracted_data"]["parties"] == ["Parent Inc.", "Target Corp."]
        assert updates.get("extracted_data", {}).get("_unsupported") is not True
        merged = {**prior, **updates}
        assert after_extraction(merged) == "compile_report"

    def test_unknown_token_extract_is_unsupported_stub(self):
        from graph.routing import after_extraction

        prior = {
            "doc_id": "d1",
            "doc_type": "unknown",
            "doc_text": "unclassifiable",
            "extraction_attempts": 0,
        }
        updates = bg.extract_node(prior)
        assert updates["extracted_data"]["_unsupported"] is True
        merged = {**prior, **updates}
        assert after_extraction(merged) == "human_review"

    def test_retry_extract_retired_class_does_not_reinvoke_specialist(self):
        from graph.routing import after_retry_extraction

        prior = {
            "doc_id": "d1",
            "doc_type": "due_diligence",
            "doc_text": "due diligence memo",
            "extracted_data": {"_unsupported": True},
            "extraction_attempts": 1,
        }
        updates = bg.retry_extract_node(prior)
        assert updates["extracted_data"]["_unsupported"] is True
        merged = {**prior, **updates}
        assert after_retry_extraction(merged) == "human_review"


class TestDispatchCoversEveryTaxonomyClass:
    def test_dispatch_keys_match_taxonomy(self):
        from pipeline.config import get_all_doc_types

        dispatch = bg._build_specialist_dispatch()
        assert set(dispatch) == set(get_all_doc_types())

    def test_unmapped_specialist_name_raises(self, monkeypatch):
        monkeypatch.setattr(
            "pipeline.config.load_config",
            lambda: {
                "doc_classes": [
                    {"key": "contract", "specialist": "no_such_agent"},
                ]
            },
        )
        try:
            bg._build_specialist_dispatch()
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "incomplete" in str(exc)
