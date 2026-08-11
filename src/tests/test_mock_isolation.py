"""Guarantee that live runs can never resolve LLM clients against fake
credentials: the mock-pilot placeholder key is rejected at the provider choke
point every entrypoint passes through, and the pilot scripts refuse to default
into mock mode.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm.providers import resolve_provider

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root

AGENT_CFG = {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}


class TestProviderRejectsMockCredentials:
    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY")
        with pytest.raises(ValueError, match="OpenRouter API key not set"):
            resolve_provider(AGENT_CFG)

    def test_mock_placeholder_key_raises(self, monkeypatch):
        # The historical "mock-key" placeholder must never authenticate a real
        # run — the watcher, API, ops monitor, and --real pilot runs all pass
        # through resolve_provider.
        monkeypatch.setenv("OPENROUTER_API_KEY", "mock-key")
        with pytest.raises(ValueError, match="mock placeholder"):
            resolve_provider(AGENT_CFG)

    def test_real_key_resolves(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-real-test")
        provider, model = resolve_provider(AGENT_CFG)
        assert provider.name == "openrouter"
        assert model == AGENT_CFG["model"]


def _env_no_dotenv() -> dict:
    """Subprocess env that mirrors the repo but cannot pick up .env overrides
    (real env vars win in load_dotenv), so each test controls the key itself."""
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["OBSERVABILITY_PROVIDER"] = "none"
    return env


class TestPilotScriptsRequireExplicitMode:
    def test_run_pilot_refuses_no_flag(self):
        proc = subprocess.run(
            [sys.executable, "src/scripts/run_pilot.py"],
            capture_output=True,
            text=True,
            env=_env_no_dotenv(),
            cwd=REPO_ROOT,
        )
        assert proc.returncode != 0
        assert "--mock (deterministic fake LLM) or --real" in proc.stderr

    def test_run_pilot_real_refuses_mock_placeholder_key(self):
        env = _env_no_dotenv()
        env["OPENROUTER_API_KEY"] = "mock-key"
        proc = subprocess.run(
            [sys.executable, "src/scripts/run_pilot.py", "--real"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )
        assert proc.returncode != 0
        assert "refusing to run in --real mode" in proc.stderr

    def test_run_quality_judges_refuses_no_flag(self):
        proc = subprocess.run(
            [sys.executable, "src/scripts/run_quality_judges.py"],
            capture_output=True,
            text=True,
            env=_env_no_dotenv(),
            cwd=REPO_ROOT,
        )
        assert proc.returncode != 0
        assert "--mock (deterministic fake judge) or --real" in proc.stderr
