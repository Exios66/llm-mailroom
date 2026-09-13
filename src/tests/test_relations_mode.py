"""HUB-052 relations-mode toggle tests — hermetic (tmp taxonomy + .env).

Covers: the mode readout (effective posture + knobs), the surgical taxonomy
editor (llm/model flips, comments and other blocks preserved byte-for-byte,
missing-key insertion), the free-only guardrail refusal, the stale .env
kill-switch removal, unknown-model refusal, the CLI surface, and the API
endpoints (GET readout, POST apply, auth + bad-mode handling).
"""

from __future__ import annotations

import os

import pytest

import pipeline.relations_mode as M


@pytest.fixture
def taxonomy_tmp(temp_base_dir):
    """A tmp taxonomy.yaml with a realistic relations block (comments +
    other sections), redirected via the module's TAXONOMY_PATH seam."""
    dst = temp_base_dir / "taxonomy.yaml"
    dst.write_text(
        """# HUB-040 relations layer — the mailroom's research clerk: deterministic
# association scanning over the archive. `llm: false` keeps the pilot
# deterministic-only; flip true in production.
cost_models:
  z-ai/glm-5.2:free:
    input_per_million: 0.0
    output_per_million: 0.0
  deepseek/deepseek-v4-flash:
    input_per_million: 0.05
    output_per_million: 0.25
relations:
  enabled: true
  llm: false
  context_injection: true
  graphs: true
  similarity_threshold: 0.62

agents:
  # HUB-040 relations judgment pass — OFF in the pilot.
  relations:
    provider: openrouter
    model: z-ai/glm-5.2:free
    temperature: 0.1
    max_tokens: 900

  sorter:
    provider: openrouter
    model: qwen/qwen3.7-flash
""",
        encoding="utf-8",
    )
    monkeypatch_path = M.TAXONOMY_PATH
    M.TAXONOMY_PATH = dst
    try:
        yield dst
    finally:
        M.TAXONOMY_PATH = monkeypatch_path


@pytest.fixture
def env_tmp(temp_base_dir):
    env_path = temp_base_dir / ".env"
    monkeypatch_path = M.ENV_PATH
    M.ENV_PATH = env_path
    try:
        yield env_path
    finally:
        M.ENV_PATH = monkeypatch_path


def _reload_config():
    import importlib

    from pipeline import config

    config.load_config.cache_clear()
    try:
        import pipeline.bins as bins

        bins._config = None
    except Exception:
        pass
    return importlib.reload(M) if False else None


class TestModeStatus:
    def test_status_reads_pilot_defaults(self, taxonomy_tmp, monkeypatch):
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
        monkeypatch.delenv("MAILROOM_RELATIONS_LLM", raising=False)
        s = M.mode_status()
        assert s["mode"] == "pilot"
        assert s["llm"] is False
        assert s["llm_effective"] is False
        assert s["enabled"] is True
        assert s["model"] == "z-ai/glm-5.2:free"
        assert s["model_is_free"] is True
        assert s["free_only_guardrail"] is False
        assert s["similarity_threshold"] == 0.62

    def test_status_live_effective(self, taxonomy_tmp, monkeypatch):
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
        monkeypatch.delenv("MAILROOM_RELATIONS_LLM", raising=False)
        M.set_mode("live")
        s = M.mode_status()
        assert s["mode"] == "live"
        assert s["llm"] is True and s["llm_effective"] is True

    def test_status_env_kill_switch_blocks(self, taxonomy_tmp, monkeypatch):
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
        monkeypatch.setenv("MAILROOM_RELATIONS_LLM", "0")
        M.set_mode("live")
        s = M.mode_status()
        assert s["llm"] is True
        assert s["llm_effective"] is False
        assert s["llm_env_blocked"] is True


class TestTaxonomyEditing:
    def test_set_live_flips_llm_preserving_everything_else(self, taxonomy_tmp, env_tmp):
        M.set_mode("live")
        text = taxonomy_tmp.read_text(encoding="utf-8")
        assert "  llm: true" in text
        assert "  llm: false" not in text
        # Comments + sibling lines preserved verbatim.
        assert "the mailroom's research clerk" in text
        assert "  enabled: true" in text
        assert "    model: z-ai/glm-5.2:free" in text
        assert "    model: qwen/qwen3.7-flash" in text

    def test_set_pilot_flips_back(self, taxonomy_tmp, env_tmp):
        M.set_mode("live")
        M.set_mode("pilot")
        text = taxonomy_tmp.read_text(encoding="utf-8")
        assert "  llm: false" in text

    def test_set_model_under_agents_relations_only(self, taxonomy_tmp, env_tmp):
        M.set_mode("live", model="deepseek/deepseek-v4-flash")
        text = taxonomy_tmp.read_text(encoding="utf-8")
        # Only the relations agent's model line changed — the sorter's line
        # stays untouched; cost_models keeps its own entry.
        assert "    model: deepseek/deepseek-v4-flash" in text
        assert "    model: qwen/qwen3.7-flash" in text
        relations_block = text.split("agents:")[1]
        assert "z-ai/glm-5.2:free" not in relations_block.split("sorter:")[0]

    def test_free_model_accepted_under_guardrail(self, taxonomy_tmp, env_tmp, monkeypatch):
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        M.set_mode("live", model="z-ai/glm-5.2:free")
        assert "  llm: true" in taxonomy_tmp.read_text(encoding="utf-8")

    def test_paid_model_refused_under_guardrail(self, taxonomy_tmp, env_tmp, monkeypatch):
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        with pytest.raises(ValueError, match="free-only pilot gate blocks"):
            M.set_mode("live", model="deepseek/deepseek-v4-flash")

    def test_unknown_model_refused(self, taxonomy_tmp, env_tmp):
        with pytest.raises(ValueError, match="unknown model"):
            M.set_mode("live", model="made-up/model")

    def test_invalid_mode_refused(self, taxonomy_tmp):
        with pytest.raises(ValueError, match="must be 'pilot' or 'live'"):
            M.set_mode("production")

    def test_stale_env_kill_switch_removed(self, taxonomy_tmp, env_tmp, monkeypatch):
        env_tmp.write_text("MAILROOM_RELATIONS_LLM=0\nGMAIL_ADDRESS=x@example.com\n", encoding="utf-8")
        # Simulate the loaded state: load_env() would have brought the .env
        # line into os.environ at process start.
        monkeypatch.setenv("MAILROOM_RELATIONS_LLM", "0")
        result = M.set_mode("live")
        assert result["env_switch_removed"] is True
        text = env_tmp.read_text(encoding="utf-8")
        assert "MAILROOM_RELATIONS_LLM" not in text
        assert "GMAIL_ADDRESS=x@example.com" in text  # other lines preserved

    def test_missing_llm_key_inserted(self, taxonomy_tmp, env_tmp):
        taxonomy_tmp.write_text(
            "relations:\n  enabled: true\n  similarity_threshold: 0.62\n",
            encoding="utf-8",
        )
        M.set_mode("live")
        text = taxonomy_tmp.read_text(encoding="utf-8")
        assert "  llm: true" in text
        assert "  similarity_threshold: 0.62" in text


class TestCli:
    def test_cli_status_exit_zero(self, taxonomy_tmp, monkeypatch, capsys):
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
        rc = M.main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PILOT" in out
        assert "judge model" in out

    def test_cli_live_and_pilot(self, taxonomy_tmp, env_tmp, monkeypatch, capsys):
        monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
        assert M.main(["live"]) == 0
        assert "  llm: true" in taxonomy_tmp.read_text(encoding="utf-8")
        assert M.main(["pilot"]) == 0
        assert "  llm: false" in taxonomy_tmp.read_text(encoding="utf-8")

    def test_cli_refusal_exit_one(self, taxonomy_tmp, env_tmp, monkeypatch, capsys):
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        assert M.main(["live", "--model", "deepseek/deepseek-v4-flash"]) == 1
        out = capsys.readouterr().out
        assert "refused" in out


class TestRelationsModeApi:
    @pytest.fixture
    def client(self, monkeypatch, temp_base_dir, taxonomy_tmp, env_tmp):
        import importlib
        import sys

        from fastapi.testclient import TestClient

        monkeypatch.setenv("MAILROOM_API_TOKEN", "test-token-123")
        monkeypatch.setenv("MAILROOM_BASE_DIR", str(temp_base_dir))
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        monkeypatch.setenv("MAILROOM_RELATIONS", "1")
        monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
        monkeypatch.delenv("MAILROOM_RELATIONS_LLM", raising=False)
        for mod in ("api.main",):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
        from api.main import app

        with TestClient(app) as c:
            yield c

    @staticmethod
    def _auth():
        return {"Authorization": "Bearer test-token-123"}

    def test_get_requires_token(self, client):
        assert client.get("/api/relations/mode").status_code == 401

    def test_get_readout(self, client):
        r = client.get("/api/relations/mode", headers=self._auth())
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] in ("pilot", "live")
        assert "model" in body
        assert "free_only_guardrail" in body

    def test_post_live_then_pilot(self, client, taxonomy_tmp):
        r = client.post(
            "/api/relations/mode",
            json={"mode": "live"},
            headers=self._auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "live"
        assert body["restart_required"] is False
        assert "  llm: true" in taxonomy_tmp.read_text(encoding="utf-8")
        r2 = client.post(
            "/api/relations/mode",
            json={"mode": "pilot"},
            headers=self._auth(),
        )
        assert r2.status_code == 200
        assert "  llm: false" in taxonomy_tmp.read_text(encoding="utf-8")

    def test_post_bad_mode_400(self, client):
        r = client.post(
            "/api/relations/mode",
            json={"mode": "production"},
            headers=self._auth(),
        )
        assert r.status_code == 400

    def test_post_paid_model_blocked_by_guardrail(self, client, monkeypatch):
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        r = client.post(
            "/api/relations/mode",
            json={"mode": "live", "model": "deepseek/deepseek-v4-flash"},
            headers=self._auth(),
        )
        assert r.status_code == 400
        assert "free-only pilot gate" in r.json()["detail"]