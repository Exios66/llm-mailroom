import asyncio
import time
from unittest.mock import MagicMock

import pytest

from graph.state import DocumentState


class TestLimitsUnit:
    def test_check_deadline_past_raises(self):
        from pipeline.limits import RunDeadlineExceeded, check_run_deadline
        with pytest.raises(RunDeadlineExceeded):
            check_run_deadline(time.time() - 5)

    def test_check_deadline_future_passes(self):
        from pipeline.limits import check_run_deadline
        check_run_deadline(time.time() + 3600)

    def test_token_budget_raises_when_cap_exceeded(self):
        from pipeline.limits import (
            RunBudgetExceeded,
            check_token_budget,
            get_max_total_output_tokens,
            record_usage,
            reset_run_usage,
        )
        reset_run_usage()
        record_usage(
            MagicMock(prompt_tokens=100, completion_tokens=get_max_total_output_tokens() + 100),
            model="qwen/qwen3.7-flash",
        )
        with pytest.raises(RunBudgetExceeded):
            check_token_budget()

    def test_token_budget_passes_under_cap(self):
        from pipeline.limits import check_token_budget, record_usage, reset_run_usage
        reset_run_usage()
        record_usage(MagicMock(prompt_tokens=100, completion_tokens=500), model="qwen/qwen3.7-flash")
        check_token_budget()  # must not raise

    def test_token_budget_ignores_large_prompt_tokens(self):
        from pipeline.limits import check_token_budget, record_usage, reset_run_usage
        reset_run_usage()
        # A 112k-char contract costs ~30k input tokens per specialist call, but
        # only a few thousand output tokens. The cap guards *generation* (stuck
        # or overly verbose models), so large prompts must never trip it.
        record_usage(
            MagicMock(prompt_tokens=100_000, completion_tokens=2_000),
            model="qwen/qwen3.7-flash",
        )
        check_token_budget()  # must not raise

    def test_token_budget_raises_on_cumulative_output(self):
        from pipeline.limits import (
            RunBudgetExceeded,
            check_token_budget,
            get_max_total_output_tokens,
            record_usage,
            reset_run_usage,
        )
        reset_run_usage()
        record_usage(
            MagicMock(prompt_tokens=100, completion_tokens=12_000),
            model="qwen/qwen3.7-flash",
        )
        record_usage(
            MagicMock(prompt_tokens=100, completion_tokens=get_max_total_output_tokens() - 8_000),
            model="qwen/qwen3.7-flash",
        )
        with pytest.raises(RunBudgetExceeded):
            check_token_budget()

    def test_record_usage_ignores_mock_usage(self):
        from pipeline.limits import record_usage, reset_run_usage, usage_summary
        reset_run_usage()
        record_usage(MagicMock())
        record_usage(None)
        assert usage_summary()["calls"] == 0
        assert usage_summary()["total"] == 0

    def test_usage_summary_aggregates(self):
        from pipeline.limits import record_usage, reset_run_usage, usage_summary
        reset_run_usage()
        record_usage(MagicMock(prompt_tokens=10, completion_tokens=20), model="m1")
        record_usage(MagicMock(prompt_tokens=30, completion_tokens=40), model="m2")
        summary = usage_summary()
        assert summary["prompt_tokens"] == 40
        assert summary["completion_tokens"] == 60
        assert summary["total"] == 100
        assert summary["calls"] == 2

    def test_estimate_cost_uses_cost_models(self):
        from pipeline.limits import estimate_cost, record_usage, reset_run_usage
        reset_run_usage()
        record_usage(MagicMock(prompt_tokens=1_000_000, completion_tokens=1_000_000), model="qwen/qwen3.7-flash")
        cost = estimate_cost()
        assert cost == pytest.approx(0.03 + 0.13, abs=1e-3)


class TestRetryTimeoutAndDeadline:
    def test_retry_applies_default_timeout(self):
        from llm.retry import retry_chat_completion
        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "hi"
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_completion

        retry_chat_completion(mock_client, model="qwen/qwen3.7-flash", messages=[{"role": "user", "content": "x"}])

        create_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert create_kwargs["timeout"] == 120

    def test_retry_deadline_blocks_before_any_attempt(self):
        from pipeline.limits import RunDeadlineExceeded
        from llm.retry import retry_chat_completion
        mock_client = MagicMock()
        with pytest.raises(RunDeadlineExceeded):
            retry_chat_completion(
                mock_client,
                model="qwen/qwen3.7-flash",
                messages=[{"role": "user", "content": "x"}],
                run_deadline=time.time() - 5,
            )
        mock_client.chat.completions.create.assert_not_called()


class TestBossBudgets:
    def _stub_llm(self, captured, monkeypatch):
        def fake_call_llm(self, user_message, **kwargs):
            if kwargs.get("max_tokens") is None:
                kwargs["max_tokens"] = self._configured_max_tokens()
            effort = kwargs.pop("reasoning_effort", None)
            if effort is None:
                effort = self._configured_reasoning_effort()
            if effort:
                kwargs["extra_body"] = {"reasoning": {"effort": effort}}
            captured.update(kwargs)
            return '{"severity": "info", "recommended_action": "none", "assessment": "ok", "findings": []}'

        monkeypatch.setattr("agents.base.BaseAgent._call_llm", fake_call_llm)

    def test_ops_analysis_uses_small_budget_no_reasoning(self, mock_openai_client, monkeypatch):
        from agents.boss import BossAgent
        captured = {}
        self._stub_llm(captured, monkeypatch)

        BossAgent().analyze_system_metrics({"stuck_documents": 0})
        assert captured["max_tokens"] == 512
        assert captured["extra_body"] == {"reasoning": {"effort": "none"}}

    def test_boss_adjudicate_keeps_max_reasoning(self, mock_openai_client, monkeypatch):
        from agents.boss import BossAgent
        captured = {}
        self._stub_llm(captured, monkeypatch)

        BossAgent().adjudicate({"doc_id": "d1", "doc_type": "contract"})
        assert captured["max_tokens"] == 8192
        assert captured["extra_body"] == {"reasoning": {"effort": "max"}}


class TestRunAbort:
    def test_run_pipeline_aborts_on_deadline_and_persists_metrics(
        self, temp_base_dir, mock_openai_client, monkeypatch
    ):
        from graph.build_graph import run_pipeline
        from pipeline.bins import failed_dir

        monkeypatch.setattr("pipeline.limits.get_deadline_seconds", lambda: -1)

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "doomed.txt"
        test_file.write_text("This run will never finish in time.")

        result = run_pipeline(test_file, matter_id="MATTER-ABORT")
        assert result["stage"] == "failed"
        assert result["run_aborted"] is True
        assert "run aborted" in (result["error_message"] or "")
        assert result["doc_id"]

        assert (failed_dir() / "doomed.txt").exists(), "file must move to failed bin"

        from storage.catalog import list_documents
        docs = asyncio.run(list_documents())
        record = next((d for d in docs if d.doc_id == result["doc_id"]), None)
        assert record is not None, "aborted run must be catalogued"
        assert record.stage == "failed"
        assert record.scores is not None
        assert record.scores["run_aborted"] == 1
        assert record.scores["run_duration_seconds"] >= 0
        assert "total_tokens" in record.scores
        assert "estimated_cost_usd" in record.scores
        assert "llm_call_count" in record.scores

    def test_full_run_records_core_metrics_offline(self, temp_base_dir, mock_openai_client):
        from graph.build_graph import run_pipeline
        from storage.catalog import list_documents

        inbox = temp_base_dir / "pipeline" / "inbox"
        test_file = inbox / "healthy.txt"
        test_file.write_text("Sample contract document for testing purposes.")

        result = run_pipeline(test_file, matter_id="MATTER-OK")
        assert result["stage"] == "archived"

        docs = asyncio.run(list_documents())
        record = next((d for d in docs if d.doc_id == result["doc_id"]), None)
        assert record is not None
        assert record.scores is not None
        for key in (
            "run_aborted",
            "run_duration_seconds",
            "total_tokens",
            "llm_call_count",
            "estimated_cost_usd",
            "classification_attempts",
            "extraction_attempts",
            "stage_completed",
            "success_rate",
        ):
            assert key in record.scores, f"missing core metric {key}"
        assert record.scores["run_aborted"] == 0
        assert record.scores["stage_completed"] == 1
        assert record.scores["success_rate"] == 1
