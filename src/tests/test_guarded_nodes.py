"""L-10/L-15: guarded Boss node + classify failure handling (audit fixes).

- L-10: boss_escalation_node must never fail the run — transient errors route
  to retry, other exceptions default review_decision="review".
- L-15: classify_node's non-transient exception path must route to human
  review (past retry_max) instead of raising into the failed bin.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
sys.path.insert(0, str(REPO_ROOT / "src"))

from graph import build_graph as bg  # noqa: E402


class TestBossGuardedNode:
    def test_boss_transient_error_returns_transient_flag(self):
        state = {
            "doc_id": "d1", "matter_id": "M", "doc_type": "contract",
            "classification_confidence": 0.9, "extraction_confidence": 0.8,
            "extracted_data": {}, "escalation_reason": "conflict",
        }
        with patch.object(bg, "_fetch_matter_context", return_value=[]):
            with patch("agents.boss.BossAgent.adjudicate") as adj:
                import openai

                adj.side_effect = openai.APIConnectionError(request=None)
                result = bg.boss_escalation_node(state)
        assert result["transient_error"] is True

    def test_boss_hard_error_defaults_to_review(self):
        state = {
            "doc_id": "d1", "matter_id": "M", "doc_type": "contract",
            "classification_confidence": 0.9, "extraction_confidence": 0.8,
            "extracted_data": {}, "escalation_reason": "conflict",
        }
        with patch.object(bg, "_fetch_matter_context", return_value=[]):
            with patch("agents.boss.BossAgent.adjudicate", side_effect=RuntimeError("boom")):
                result = bg.boss_escalation_node(state)
        assert result["review_decision"] == "review"
        assert "Boss unavailable" in result["escalation_reason"]

    def test_boss_ok_path_unchanged(self):
        state = {
            "doc_id": "d1", "matter_id": "M", "doc_type": "contract",
            "classification_confidence": 0.9, "extraction_confidence": 0.8,
            "extracted_data": {}, "escalation_reason": "conflict",
        }
        with patch.object(bg, "_fetch_matter_context", return_value=[]):
            with patch("agents.boss.BossAgent.adjudicate", return_value={"decision": "approved", "reasoning": "fine"}):
                result = bg.boss_escalation_node(state)
        assert result["review_decision"] == "approved"


class TestClassifyFailureRoutesToReview:
    def test_non_transient_classify_error_routes_to_review(self):
        state = {
            "doc_id": "d1", "matter_id": "M", "doc_text": "some text",
            "classification_attempts": 0, "doc_pages": None,
        }
        with patch("agents.sorter.SorterAgent.classify", side_effect=RuntimeError("boom")):
            result = bg.classify_node(state)
        assert result["classification_confidence"] == 0.1
        # Past retry_max → after_classify sends it to review, not failed.
        from pipeline.config import get_confidence_thresholds

        retry_max = get_confidence_thresholds().get("retry_max", 1)
        assert result["classification_attempts"] > retry_max
        assert "routing to human review" in result["escalation_reason"]
        assert result["transient_error"] is False

    def test_transient_classify_error_still_retries(self):
        state = {
            "doc_id": "d1", "matter_id": "M", "doc_text": "some text",
            "classification_attempts": 0, "doc_pages": None,
        }
        import openai

        with patch("agents.sorter.SorterAgent.classify", side_effect=openai.APIConnectionError(request=None)):
            result = bg.classify_node(state)
        assert result["transient_error"] is True
        assert result["classification_attempts"] == 0  # no retry budget burned
