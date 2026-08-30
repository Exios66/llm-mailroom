"""Production finalization, scoring completeness, and per-agent eval."""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    for mod in ("api.main",):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from api.main import app

    with TestClient(app) as c:
        yield c


class TestArchiveMissingFileFinalizes:
    def test_missing_path_moves_nothing_but_marks_failed(self, temp_base_dir, mock_openai_client):
        from graph.build_graph import archive_node, _ensure_dirs
        from pipeline.bins import load_manifest, manifests_dir

        _ensure_dirs()
        state = {
            "doc_id": "arch-miss-1",
            "matter_id": "M",
            "original_filename": "gone.txt",
            "doc_type": "contract",
            "file_path": "",
            "stage": "classified",
        }
        result = archive_node(state)
        assert result["stage"] == "failed"
        assert result.get("run_aborted") is True
        manifest = load_manifest("arch-miss-1")
        assert manifest is not None
        assert manifest.stage.value == "failed"
        assert manifests_dir().exists()

    def test_missing_file_on_disk_finalizes(self, temp_base_dir, mock_openai_client):
        from graph.build_graph import archive_node, _ensure_dirs

        _ensure_dirs()
        missing = Path(temp_base_dir) / "pipeline" / "processing" / "w1" / "nope.txt"
        missing.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "doc_id": "arch-miss-2",
            "matter_id": "M",
            "original_filename": "nope.txt",
            "doc_type": "contract",
            "file_path": str(missing),
            "stage": "classified",
        }
        result = archive_node(state)
        assert result["stage"] == "failed"
        assert "File not found" in (result.get("error_message") or "")


class TestWatcherReconcileTerminal:
    def test_terminal_manifest_retires_to_failed(self, temp_base_dir):
        import json
        import os
        import time

        from pipeline import bins

        proc = bins.processing_dir("worker-abc")
        proc.mkdir(parents=True, exist_ok=True)
        f = proc / "doc.pdf"
        f.write_bytes(b"x")
        old = time.time() - 7200
        os.utime(f, (old, old))

        mf = bins.manifests_dir() / "term.json"
        mf.write_text(json.dumps({
            "original_filename": "doc.pdf",
            "stage": "archived",
            "doc_id": "already-done",
        }))

        action, dest = bins.reconcile_stale_processing_file(f)
        assert action == "failed"
        assert dest.parent == bins.failed_dir()
        assert dest.exists()
        assert not f.exists()
        assert not (bins.inbox_dir() / "doc.pdf").exists()

    def test_no_terminal_manifest_requeues(self, temp_base_dir):
        import os
        import time

        from pipeline import bins

        proc = bins.processing_dir("worker-abc")
        proc.mkdir(parents=True, exist_ok=True)
        f = proc / "fresh-orphan.pdf"
        f.write_bytes(b"x")
        old = time.time() - 7200
        os.utime(f, (old, old))

        action, dest = bins.reconcile_stale_processing_file(f)
        assert action == "requeue"
        assert dest == bins.inbox_dir() / "fresh-orphan.pdf"


class TestHealthWatcherDegrades:
    def test_missing_watcher_degrades_status(self, client):
        r = client.get("/health")
        body = r.json()
        assert body["checks"]["watcher"] == "missing"
        assert body["status"] == "degraded"
        assert body["producer"] is True
        assert body["review_resolve"] is True
        assert body["inbox_upload"] is True

    def test_live_watcher_lamp_ok_when_deps_ok(self, client, temp_base_dir):
        from pipeline import bins

        bins.touch_watcher_heartbeat()
        r = client.get("/health")
        assert r.json()["checks"]["watcher"] == "live"


class TestPromptRegistryImageExtractor:
    def test_image_extractor_in_prompt_templates(self):
        from llm.prompts import prompt_templates

        templates = prompt_templates()
        assert "image_extractor" in templates
        assert templates["image_extractor"].strip()
        assert len(templates) == 15

    def test_image_extractor_uses_managed_prompt(self, mock_openai_client):
        from agents.image_extractor import ImageExtractor, SYSTEM_PROMPT

        agent = ImageExtractor()
        text = agent.system_prompt()
        assert "document image analyst" in text.lower() or "visible text" in text.lower()
        assert SYSTEM_PROMPT in text or text == SYSTEM_PROMPT or "PRODUCTION DOCTRINE" in text


class TestScoringCompleteness:
    def test_stage_correct_emitted_when_ground_truth_present(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        from observability.scores import emit_pipeline_scores

        hit = emit_pipeline_scores({
            "stage": "archived",
            "doc_type": "contract",
            "ground_truth": {
                "expected_doc_class": "contract",
                "expected_stage": "archived",
            },
            "extracted_data": {"parties": ["A"]},
        })
        assert hit["stage_correct"] == 1
        miss = emit_pipeline_scores({
            "stage": "review",
            "doc_type": "contract",
            "ground_truth": {"expected_stage": "archived"},
            "extracted_data": {"parties": ["A"]},
        })
        assert miss["stage_correct"] == 0

    def test_deterministic_verdict_labels(self):
        from observability.scores import deterministic_verdict_label

        assert deterministic_verdict_label(1.0, needs_judge_review=False) == "CORRECT"
        assert deterministic_verdict_label(0.0, needs_judge_review=False) == "MISS"
        assert deterministic_verdict_label(0.7, needs_judge_review=True) == "PARTIAL"
        assert deterministic_verdict_label(0.9, class_mismatch=True) == "MISS"

    def test_in_pipeline_judge_scores_noop_when_skipped(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        from observability.scores import emit_in_pipeline_judge_scores

        emit_in_pipeline_judge_scores({"judge_verdict": "skipped"})


class TestAgentEvalHarness:
    def test_list_includes_all_llm_roles(self):
        from observability.agent_eval import LLM_AGENTS

        assert "sorter" in LLM_AGENTS
        assert "image_extractor" in LLM_AGENTS
        assert "insurance_claims_specialist" in LLM_AGENTS
        assert len(LLM_AGENTS) == 13

    def test_insurance_manifest_cases_present(self):
        from observability.agent_eval import load_manifest_cases

        cases = load_manifest_cases(mock=True, doc_class="insurance_claim")
        ids = {c["id"] for c in cases}
        assert any("insurance_01" in i for i in ids)
        assert any("insurance_02" in i for i in ids)
        assert any("insurance_03" in i for i in ids)

    def test_token_overlap(self):
        from observability.agent_eval import token_overlap

        assert token_overlap("a b c", "a b c") == 1.0
        assert token_overlap("", "") == 1.0
        assert token_overlap("hello world", "goodbye") == 0.0

    def test_sorter_self_check(self):
        from observability.agent_eval import evaluate_agent

        report = evaluate_agent("sorter", mock=True, n=3, invoke=False)
        assert report["metrics"]["n"] == 3
        assert report["metrics"]["class_accuracy"] == 1.0
        assert report["errors"] == 0

    def test_insurance_specialist_self_check(self):
        from observability.agent_eval import evaluate_agent

        report = evaluate_agent(
            "insurance_claims_specialist", mock=True, n=3, invoke=False
        )
        assert report["metrics"]["n"] >= 1
        assert report["errors"] == 0

    def test_sorter_invoke_with_mock(self, mock_langchain_llm):
        mock_langchain_llm.classification = {
            "doc_type": "contract",
            "contract_subtype": "other",
            "confidence": 0.95,
            "reasoning": "mock",
        }
        from observability.agent_eval import evaluate_agent

        report = evaluate_agent("sorter", mock=True, n=1, invoke=True)
        assert report["metrics"]["n"] == 1
        assert report["errors"] == 0
        assert report["rows"][0].get("predicted_doc_class") == "contract"

    def test_cli_list(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        proc = subprocess.run(
            [sys.executable, "src/scripts/run_agent_eval.py", "--list"],
            capture_output=True, text=True, cwd=root,
            env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
        )
        assert proc.returncode == 0
        assert "sorter" in proc.stdout
        assert "image_extractor" in proc.stdout
