"""LiteLLM gateway + Modal GPU tiers (deploy/docker-compose.full.yml, Mode G).

Covers per-agent tier routing in ``llm/client.py``, thinking-mode extra_body
shapes, vision exclusion of text-only tier aliases, the gateway cold-start
backoff, the multi-tier Modal app, the gateway/compose config contracts, and
the smoke script's generation-to-tier attribution. No network, no Docker.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from llm import providers
from llm.client import (
    check_spend_guardrail,
    gateway_tier,
    get_llm,
    reasoning_extra_body,
    resolve_agent_model,
)
from pipeline.config import load_config

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "docker-compose.full.yml"
LITELLM = REPO / "deploy" / "litellm" / "config.yaml"
SMOKE = REPO / "src" / "scripts" / "smoke_modal_tiers.py"


@pytest.fixture
def gateway(monkeypatch):
    """Route every agent through the LiteLLM gateway provider."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "litellm")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://llm-gateway:4000/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-gw")
    monkeypatch.delenv("MAILROOM_GATEWAY_TIERS", raising=False)
    monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
    providers._providers_cache = None
    yield
    providers._providers_cache = None


def _load_modal():
    from tests.test_vllm_modal_capability import _load_app_module

    return _load_app_module()


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_modal_tiers", SMOKE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke_modal_tiers"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── routing ────────────────────────────────────────────────────────────────


class TestTierRouting:
    def test_every_llm_agent_has_a_known_tier(self):
        cfg = load_config()
        tiers = cfg["gateway"]["tiers"]
        for name, agent in cfg["agents"].items():
            if agent.get("procedural") or not agent.get("model"):
                continue
            assert agent.get("tier") in tiers, f"{name} has no valid gateway tier"

    def test_fast_agent_gets_fast_alias(self, gateway):
        r = resolve_agent_model("sorter")
        assert (r.provider.name, r.tier, r.model) == ("litellm", "fast", "mailroom-fast")
        assert r.on_gpu_tier and r.served_by_vllm and r.spend_exempt

    def test_extract_agent_gets_extract_alias(self, gateway):
        r = resolve_agent_model("contracts_specialist")
        assert (r.tier, r.model) == ("extract", "mailroom-extract")

    def test_get_llm_uses_gateway_endpoint(self, gateway):
        client, model = get_llm("boss")
        assert model == "mailroom-extract"
        assert "llm-gateway:4000" in str(client.base_url)

    def test_env_override_to_api_sends_champion(self, gateway, monkeypatch):
        monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=api, boss=fast")
        r = resolve_agent_model("sorter")
        assert r.tier == "api" and r.model == r.champion
        assert not r.on_gpu_tier and not r.served_by_vllm
        assert resolve_agent_model("boss").model == "mailroom-fast"

    def test_unknown_tier_raises(self, gateway, monkeypatch):
        monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=gpu-typo")
        with pytest.raises(ValueError, match="unknown gateway tier"):
            gateway_tier("sorter")

    def test_non_gateway_provider_untouched(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
        providers._providers_cache = None
        r = resolve_agent_model("sorter")
        assert r.tier is None and r.model == r.champion

    def test_free_only_exempts_gpu_tier_but_guards_api_tier(self, gateway, monkeypatch):
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        check_spend_guardrail("sorter", resolve_agent_model("sorter"))  # GPU: no raise
        monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=api")
        r = resolve_agent_model("sorter")
        if r.model.endswith(":free") or r.model == "openrouter/free":
            pytest.skip("sorter champion is free")
        with pytest.raises(RuntimeError, match="not free"):
            check_spend_guardrail("sorter", r)


class TestReasoningExtraBody:
    def test_none_effort_is_none(self):
        assert reasoning_extra_body(None, "sorter") is None

    def test_openrouter_shape_without_gateway(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
        providers._providers_cache = None
        assert reasoning_extra_body("low", "sorter") == {"reasoning": {"effort": "low"}}

    def test_gpu_tier_disables_thinking(self, gateway):
        assert reasoning_extra_body("none", "sorter") == {
            "chat_template_kwargs": {"enable_thinking": False}
        }
        assert reasoning_extra_body("high", "sorter") == {
            "chat_template_kwargs": {"enable_thinking": True}
        }

    def test_api_tier_keeps_openrouter_shape(self, gateway, monkeypatch):
        monkeypatch.setenv("MAILROOM_GATEWAY_TIERS", "sorter=api")
        assert reasoning_extra_body("low", "sorter") == {"reasoning": {"effort": "low"}}


class TestVisionAndRetry:
    def test_text_tiers_not_vision_capable(self):
        from llm.vision import is_vision_capable

        assert is_vision_capable("mailroom-vision")
        assert not is_vision_capable("mailroom-fast")
        assert not is_vision_capable("mailroom-extract")
        assert not is_vision_capable("qwen/qwen3-8b")

    def test_gateway_503_uses_cold_start_backoff(self, gateway):
        from llm.retry import retry_sleep_seconds

        class Exc(Exception):
            status_code = 503

        cfg = {"modal_cold_start_delay": 90.0, "modal_cold_start_max_delay": 240.0, "jitter": 0}
        assert retry_sleep_seconds(Exc(), 1, cfg, base_url="http://llm-gateway:4000/v1") >= 90
        assert retry_sleep_seconds(Exc(), 1, {**cfg, "max_delay": 30.0}, base_url="https://openrouter.ai/api/v1") <= 30


# ── Modal multi-tier app ───────────────────────────────────────────────────


class TestModalTiers:
    def test_tier_aliases_match_taxonomy(self):
        mod = _load_modal()
        gw = load_config()["gateway"]["tiers"]
        for name, spec in mod.TIERS.items():
            assert gw[name]["alias"] == spec.alias
            assert gw[name]["context_tokens"] == int(spec.max_model_len)

    def test_tier_command_flags(self):
        mod = _load_modal()
        cmd = mod.build_vllm_command(mod.TIERS["vision"].model, mod.TIERS["vision"])
        i = cmd.index("--served-model-name")
        assert cmd[i + 1] == "mailroom-vision"
        assert "--limit-mm-per-prompt" in cmd
        fast = mod.build_vllm_command(mod.TIERS["fast"].model, mod.TIERS["fast"])
        assert "--reasoning-parser" in fast

    def test_scoped_knob_overrides_one_tier(self, monkeypatch):
        monkeypatch.setenv("MODAL_VLLM_EXTRACT_MAX_MODEL_LEN", "131072")
        mod = _load_modal()
        assert int(mod.resolve_tier("extract").max_model_len) == 131072
        assert int(mod.resolve_tier("fast").max_model_len) == 32768

    def test_legacy_knob_applies_to_fast_only(self, monkeypatch):
        monkeypatch.setenv("MODAL_VLLM_MODEL", "Qwen/Qwen3-4B")
        mod = _load_modal()
        assert mod.resolve_tier("fast").model == "Qwen/Qwen3-4B"
        assert mod.resolve_tier("extract").model != "Qwen/Qwen3-4B"

    def test_tier_url_env(self):
        assert _load_modal().tier_url_env("extract") == "MODAL_EXTRACT_URL"


# ── gateway + compose contracts ────────────────────────────────────────────


class TestDeployContracts:
    def test_litellm_routes_every_alias_and_no_callbacks(self):
        cfg = yaml.safe_load(LITELLM.read_text())
        names = {m["model_name"] for m in cfg["model_list"]}
        aliases = {t["alias"] for t in load_config()["gateway"]["tiers"].values() if t.get("alias")}
        assert aliases <= names and "*" in names
        assert cfg["litellm_settings"]["num_retries"] == 0
        assert cfg["router_settings"]["num_retries"] == 0
        assert "callbacks" not in cfg["litellm_settings"]
        assert "success_callback" not in cfg["litellm_settings"]

    def test_compose_healthcheck_gating(self):
        svc = yaml.safe_load(COMPOSE.read_text())["services"]
        assert {"postgres", "llm-gateway", "app", "ops-monitor", "watchdog", "phoenix"} <= set(svc)
        deps = svc["app"]["depends_on"]
        assert deps["postgres"]["condition"] == "service_healthy"
        assert deps["llm-gateway"]["condition"] == "service_healthy"
        assert deps["phoenix"]["required"] is False
        for s in ("ops-monitor", "watchdog"):
            assert svc[s]["depends_on"]["app"]["condition"] == "service_healthy"
            assert svc[s]["healthcheck"]["disable"] is True
        assert svc["watchdog"]["pid"] == "service:app"
        env = svc["app"]["environment"]
        assert env["DEFAULT_PROVIDER"] == "litellm"
        assert env["LITELLM_BASE_URL"] == "http://llm-gateway:4000/v1"
        assert env["MAILROOM_EMBED_WATCHER"] == "1"
        assert "postgresql+psycopg://" in env["DATABASE_URL"]
        image = svc["llm-gateway"]["image"]
        assert image == "${LITELLM_IMAGE:-ghcr.io/berriai/litellm:v1.104.0}"
        assert "main-stable" not in image
        assert "v1.104.0-stable" not in image

    def test_compose_documents_the_litellm_image_pin(self):
        # docs/ is the single source of truth; deploy/README.md is a file index.
        guide = (REPO / "docs" / "docker-deployment.md").read_text(encoding="utf-8")
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        assert "ghcr.io/berriai/litellm:v1.104.0" in guide
        assert "Do **not** use `main-stable`" in guide
        assert "LITELLM_IMAGE=ghcr.io/berriai/litellm:v1.104.0" in example
        assert "do not use main-stable" in example


# ── smoke script attribution ───────────────────────────────────────────────


class TestSmokeScript:
    @pytest.fixture
    def smoke(self):
        return _load_smoke()

    @pytest.fixture
    def routing(self, smoke):
        return smoke.expected_routing(load_config())

    def test_check_contract_ok(self, smoke):
        failures, routing = smoke.check_contract(load_config())
        assert failures == []
        assert routing["sorter"]["tier"] == "fast"

    def _obs(self):
        return [
            {"id": "n1", "type": "SPAN", "name": "classify-document"},
            {"id": "n2", "type": "SPAN", "name": "extract-fields"},
            {"id": "g1", "type": "GENERATION", "name": "sorter", "model": "mailroom-fast", "parentObservationId": "n1"},
            {"id": "c1", "type": "SPAN", "name": "ChatOpenAI", "parentObservationId": "n2"},
            {"id": "g2", "type": "GENERATION", "name": "ChatOpenAI", "model": "mailroom-extract", "parentObservationId": "c1"},
            {"id": "g3", "type": "GENERATION", "name": "pipeline-result", "model": None},
        ]

    def test_verify_generations_pass(self, smoke, routing):
        rows, failures = smoke.verify_generations(self._obs(), routing, "contracts_specialist")
        assert failures == []
        assert {r["node"] for r in rows} == {"classify-document", "extract-fields"}
        assert all(r["generation"] != "pipeline-result" for r in rows)

    def test_verify_generations_flags_wrong_tier(self, smoke, routing):
        obs = self._obs()
        obs[2]["model"] = "mailroom-extract"  # sorter must be on the fast tier
        _, failures = smoke.verify_generations(obs, routing, "contracts_specialist")
        assert len(failures) == 1 and "classify-document" in failures[0]

    def test_pick_documents_closest_length(self, smoke):
        rows = [
            {"class": "contract", "doc_text": "x" * 100, "filename": "a"},
            {"class": "contract", "doc_text": "x" * 950, "filename": "b"},
            {"class": "correspondence", "doc_text": "y" * 50, "filename": "c"},
        ]
        picked = smoke.pick_documents(rows, ["contract", "correspondence", "insurance_claim"], 1000)
        assert picked["contract"]["filename"] == "b"
        assert "insurance_claim" not in picked


class TestSmokePolling:
    @pytest.mark.parametrize("failure", ["status", "request", "json"])
    @pytest.mark.parametrize("recover", [True, False])
    def test_transient_failure_preserves_results_and_deadline(self, monkeypatch, tmp_path, failure, recover):
        import httpx

        smoke = _load_smoke()
        files = {cls: tmp_path / f"{cls}.txt" for cls in ("contract", "correspondence")}
        for path in files.values():
            path.write_text("test document")
        completed = {
            "original_filename": "contract.txt", "stage": "archived",
            "doc_type": "contract", "doc_id": "doc-1",
        }
        pending = {
            "original_filename": "correspondence.txt", "stage": "review",
            "doc_type": "correspondence", "doc_id": "doc-2",
        }
        now = [0]
        sleeps = []
        uploads = iter(files.values())
        polls = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        def handle(request):
            if request.method == "POST":
                assert request.url.path == "/upload"
                return httpx.Response(200, json={"file": next(uploads).name})
            assert request.url.path == "/matters/test-matter"
            polls.append(now[0])
            if len(polls) == 1:
                return httpx.Response(200, json={"documents": [completed]})
            if recover and len(polls) == 3:
                return httpx.Response(200, json={"documents": [pending]})
            if failure == "status":
                # Even a JSON error response must not count as completion.
                return httpx.Response(503, json={"documents": [pending]})
            if failure == "request":
                raise httpx.ReadTimeout("temporary failure", request=request)
            return httpx.Response(200, text="not JSON")

        client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test")
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
        monkeypatch.setattr(smoke.time, "time", lambda: now[0])
        monkeypatch.setattr(smoke.time, "sleep", sleep)

        results = smoke.run_via_api("http://test", "", files, "test-matter", timeout_s=30)

        assert results["contract"] == {
            "stage": "archived", "doc_type": "contract", "doc_id": "doc-1", "filename": "contract.txt",
        }
        assert results["correspondence"] == {
            "stage": "review" if recover else "timeout",
            "doc_type": "correspondence" if recover else None,
            "doc_id": "doc-2" if recover else None,
            "filename": "correspondence.txt",
        }
        assert polls == [0, 10, 20]
        assert sleeps == [10] * (2 if recover else 3)
