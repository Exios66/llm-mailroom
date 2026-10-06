"""KANBAN-064 — Modal+vLLM offline serving capability tests.

Network-free by construction:
- deploy/modal_vllm.py is loaded with a stubbed `modal` module (the real one
  is a deploy-time extra, never installed in the runtime venv);
- provider-seam tests exercise llm/providers.py + llm/client.py directly with
  monkeypatched env — no HTTP, no server, no API keys.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

DEPLOY_APP = Path(__file__).resolve().parents[2] / "deploy" / "modal_vllm.py"


def _install_modal_stub() -> None:
    """Minimal stand-in for the `modal` module surface used by the app."""
    if "modal" in sys.modules:
        return
    stub = types.ModuleType("modal")

    class _Secret:
        @staticmethod
        def from_dict(mapping):
            return ("secret", mapping)

    class _Volume:
        @staticmethod
        def from_name(name, create_if_missing=False):
            return ("volume", name)

    class _Image:
        @staticmethod
        def from_registry(ref, add_python=None):
            return _Image()

        @staticmethod
        def debian_slim(python_version=None):
            return _Image()

        def run_commands(self, *cmds):
            return self

        def uv_pip_install(self, *pkgs):
            return self

        def env(self, mapping):
            return self

    class _App:
        def __init__(self, name, image=None, tags=None):
            self.name = name
            self.tags = tags or {}

        def function(self, **kwargs):
            def deco(fn):
                return fn

            return deco

        def local_entrypoint(self, fn=None):
            if fn is not None:
                return fn

            def deco(f):
                return f

            return deco

    def _web_server(port=None, startup_timeout=None, label=None):
        def deco(fn):
            fn._web_label = label
            return fn

        return deco

    def _concurrent(max_inputs=None, target_inputs=None):
        def deco(fn):
            fn._max_inputs = max_inputs
            return fn

        return deco

    stub.Secret = _Secret
    stub.Volume = _Volume
    stub.Image = _Image
    stub.App = _App
    stub.web_server = _web_server
    stub.concurrent = _concurrent
    sys.modules["modal"] = stub


def _load_app_module():
    _install_modal_stub()
    spec = importlib.util.spec_from_file_location("mailroom_modal_vllm", DEPLOY_APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestModalVllmApp:
    def test_app_file_exists(self):
        assert DEPLOY_APP.is_file(), "deploy/modal_vllm.py missing"

    def test_command_defaults(self):
        mod = _load_app_module()
        cmd = mod.build_vllm_command("Qwen/Qwen3-8B")
        assert cmd[:3] == ["vllm", "serve", "Qwen/Qwen3-8B"]
        assert "--host" in cmd and cmd[cmd.index("--host") + 1] == "0.0.0.0"
        assert "--port" in cmd and cmd[cmd.index("--port") + 1] == str(mod.SERVER_PORT)
        assert "--max-model-len" in cmd
        # fp16 default: no quantization flag unless configured
        assert "--quantization" not in cmd

    def test_quantization_flag_injected_when_configured(self):
        mod = _load_app_module()
        original = mod.QUANTIZATION
        try:
            mod.QUANTIZATION = "awq"
            cmd = mod.build_vllm_command("Qwen/Qwen3-14B")
            assert "--quantization" in cmd
            assert cmd[cmd.index("--quantization") + 1] == "awq"
        finally:
            mod.QUANTIZATION = original

    def test_api_token_maps_to_vllm_enforcement_var(self, monkeypatch):
        mod = _load_app_module()
        monkeypatch.setenv("MODAL_VLLM_API_TOKEN", "tok-abc123")
        env = mod._server_env()
        assert env["VLLM_API_KEY"] == "tok-abc123"

    def test_no_token_means_keyless_server(self, monkeypatch):
        mod = _load_app_module()
        monkeypatch.delenv("MODAL_VLLM_API_TOKEN", raising=False)
        env = mod._server_env()
        assert "VLLM_API_KEY" not in env

    def test_hf_token_passthrough_for_gated_repos(self, monkeypatch):
        mod = _load_app_module()
        monkeypatch.setenv("HF_TOKEN", "hf_xxx")
        assert mod._server_env()["HF_TOKEN"] == "hf_xxx"
        monkeypatch.delenv("HF_TOKEN")
        assert "HF_TOKEN" not in mod._server_env()


class TestVllmProviderSeam:
    """The runtime half: DEFAULT_PROVIDER=vllm must reach get_llm untouched."""

    def _fresh_providers(self):
        import llm.providers as providers

        providers._providers_cache = None  # rebuild from current env
        return providers

    def test_vllm_provider_supports_optional_bearer(self, monkeypatch):
        providers = self._fresh_providers()
        monkeypatch.delenv("VLLM_API_KEY", raising=False)
        provider, model = providers.resolve_provider(
            {"provider": "vllm", "model": "Qwen/Qwen3-8B"}
        )
        assert provider.api_key_env == "VLLM_API_KEY"
        assert provider.base_url.startswith("http://localhost:8000")
        assert model == "Qwen/Qwen3-8B"  # wildcard catalog: any model passes through

    def test_base_url_env_override(self, monkeypatch):
        providers = self._fresh_providers()
        monkeypatch.setenv(
            "VLLM_BASE_URL", "https://workspace--mailroom-vllm-serve.modal.run/v1"
        )
        provider, _ = providers.resolve_provider({"provider": "vllm"})
        assert provider.base_url == (
            "https://workspace--mailroom-vllm-serve.modal.run/v1"
        )

    def test_default_provider_env_wins_over_agent_config(self, monkeypatch):
        providers = self._fresh_providers()
        monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
        provider, _ = providers.resolve_provider(
            {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}
        )
        assert provider.name == "vllm"

    def test_openrouter_primary_unchanged_by_default(self, monkeypatch):
        """The capability must not move the default serving path."""
        providers = self._fresh_providers()
        monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
        provider, model = providers.resolve_provider(
            {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}
        )
        assert provider.name == "openrouter"
        assert model == "qwen/qwen3.7-flash"

    def test_get_llm_end_to_end_on_vllm_provider(self, monkeypatch):
        import llm.client as client_mod

        providers = self._fresh_providers()
        monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
        monkeypatch.setenv("VLLM_API_KEY", "tok-end2end")
        monkeypatch.setattr(
            client_mod, "get_agent_config", lambda name: {"model": "*"}
        )
        # Tracing instrumentation is orthogonal here; identity-stub it so the
        # test stays hermetic regardless of which backends are configured.
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        got, model = client_mod.get_llm("sorter")
        assert str(got.base_url).startswith("http://localhost:8000")
        assert got.api_key == "tok-end2end"

    def test_get_llm_keyless_when_no_token(self, monkeypatch):
        import llm.client as client_mod

        providers = self._fresh_providers()
        monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
        monkeypatch.delenv("VLLM_API_KEY", raising=False)
        monkeypatch.setattr(client_mod, "get_agent_config", lambda name: {"model": "*"})
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        got, _ = client_mod.get_llm("sorter")
        assert got.api_key == "not-needed"

    def test_vllm_champion_ids_remap_to_served_ids(self, monkeypatch):
        """DMR-052: the taxonomy's OpenRouter slug must become the served HF id."""
        import llm.client as client_mod

        self._fresh_providers()
        monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
        monkeypatch.setattr(
            client_mod, "get_agent_config", lambda name: {"model": "qwen/qwen3.7-flash"}
        )
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        _, model = client_mod.get_llm("sorter")
        assert model == "Qwen/Qwen3-8B"

    def test_free_only_guardrail_exempts_self_hosted(self, monkeypatch):
        """DMR-052: MAILROOM_LLM_FREE_ONLY bounds OpenRouter spend — a vLLM
        run must resolve even though the served id is not a ':free' model."""
        import llm.client as client_mod

        self._fresh_providers()
        monkeypatch.setenv("DEFAULT_PROVIDER", "vllm")
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        monkeypatch.setattr(
            client_mod, "get_agent_config", lambda name: {"model": "Qwen/Qwen3-8B"}
        )
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        got, _ = client_mod.get_llm("sorter")  # must not raise
        assert str(got.base_url).startswith("http://localhost:8000")

    def test_free_only_still_refuses_non_openrouter_hosted(self, monkeypatch):
        import llm.client as client_mod

        self._fresh_providers()
        monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
        monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-real")
        monkeypatch.setattr(
            client_mod,
            "get_agent_config",
            lambda name: {"model": "deepseek/deepseek-v4-pro"},
        )
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        with pytest.raises(RuntimeError, match="not free"):
            client_mod.get_llm("sorter")


class TestModalTierBoundaries:
    @pytest.fixture
    def snapshot_download(self, monkeypatch, mocker):
        # huggingface_hub is installed in Modal's image, not the test runtime.
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = mocker.Mock()
        monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
        return hub.snapshot_download

    @pytest.fixture
    def tier_app(self, monkeypatch):
        import os

        # Tier settings are read at import time; developer deployment knobs
        # must not alter these unit tests or escape into the Modal stub.
        for name in list(os.environ):
            if name.startswith("MODAL_VLLM_") or name == "HF_TOKEN":
                monkeypatch.delenv(name)
        monkeypatch.delitem(sys.modules, "modal", raising=False)
        _install_modal_stub()
        stub = sys.modules.pop("modal")
        monkeypatch.setitem(sys.modules, "modal", stub)
        return _load_app_module()

    @pytest.mark.parametrize("scoped,expected", [(" scoped/model ", "scoped/model"), ("  ", "legacy/model")])
    def test_scoped_model_precedes_legacy_with_blank_fallback(self, tier_app, monkeypatch, scoped, expected):
        monkeypatch.setenv("MODAL_VLLM_MODEL", "legacy/model")
        monkeypatch.setenv("MODAL_VLLM_FAST_MODEL", scoped)
        assert tier_app.resolve_tier("fast").model == expected
        assert tier_app.resolve_tier("extract").model == tier_app.TIERS["extract"].model

    @pytest.mark.parametrize("gpu,override,expected", [
        ("A100-80GB:2", None, 2), ("H100:4", "2", 2),
        ("L4", None, 1), ("L4:invalid", None, 1),
    ])
    def test_tensor_parallelism_follows_gpu_unless_overridden(self, tier_app, monkeypatch, gpu, override, expected):
        monkeypatch.setenv("MODAL_VLLM_EXTRACT_GPU", gpu)
        if override is not None:
            monkeypatch.setenv("MODAL_VLLM_EXTRACT_TP_SIZE", override)
        spec = tier_app.resolve_tier("extract")
        assert spec.tp_size == expected
        cmd = tier_app.build_vllm_command(spec.model, spec)
        if expected == 1:
            assert "--tensor-parallel-size" not in cmd
        else:
            assert cmd[cmd.index("--tensor-parallel-size") + 1] == str(expected)

    def test_parser_none_disables_default_reasoning_parser(self, tier_app, monkeypatch):
        monkeypatch.setenv("MODAL_VLLM_FAST_REASONING_PARSER", " NoNe ")
        spec = tier_app.resolve_tier("fast")
        assert spec.reasoning_parser == ""
        assert "--reasoning-parser" not in tier_app.build_vllm_command(spec.model, spec)

    def test_custom_tier_options_reach_server_command(self, tier_app, monkeypatch):
        knobs = {
            "MODEL": "custom/vision", "REVISION": "pinned-revision", "QUANTIZATION": "awq",
            "MAX_MODEL_LEN": "16384", "GPU_MEMORY_UTILIZATION": "0.75",
            "MAX_NUM_SEQS": "8", "LIMIT_IMAGES": "3", "MAX_INPUTS": "7",
            "MAX_CONTAINERS": "2", "MIN_CONTAINERS": "1", "SCALEDOWN_SECONDS": "60",
        }
        for knob, value in knobs.items():
            monkeypatch.setenv(f"MODAL_VLLM_VISION_{knob}", value)
        spec = tier_app.resolve_tier("vision")
        cmd = tier_app.build_vllm_command(spec.model, spec)
        assert cmd[:3] == ["vllm", "serve", "custom/vision"]
        alias_pos = cmd.index("--served-model-name")
        assert cmd[alias_pos + 1:alias_pos + 3] == ["mailroom-vision", "custom/vision"]
        for flag, expected in {
            "--revision": "pinned-revision", "--quantization": "awq",
            "--max-model-len": "16384", "--gpu-memory-utilization": "0.75",
            "--max-num-seqs": "8", "--limit-mm-per-prompt": '{"image": 3}',
        }.items():
            assert cmd[cmd.index(flag) + 1] == expected
        assert (spec.max_inputs, spec.max_containers, spec.min_containers, spec.scaledown_seconds) == (7, 2, 1, 60)

    def test_zero_image_budget_omits_multimodal_flag(self, tier_app, monkeypatch):
        monkeypatch.setenv("MODAL_VLLM_VISION_LIMIT_IMAGES", "0")
        spec = tier_app.resolve_tier("vision")
        assert spec.limit_images == 0
        assert "--limit-mm-per-prompt" not in tier_app.build_vllm_command(spec.model, spec)

    @pytest.mark.parametrize("knob", ["MAX_MODEL_LEN", "TP_SIZE", "MAX_INPUTS", "LIMIT_IMAGES"])
    def test_malformed_integer_knobs_fail_loudly(self, tier_app, monkeypatch, knob):
        monkeypatch.setenv(f"MODAL_VLLM_FAST_{knob}", "many")
        with pytest.raises(ValueError):
            tier_app.resolve_tier("fast")

    def test_unknown_tier_fails_loudly(self, tier_app):
        with pytest.raises(ValueError, match="unknown tier 'typo'"):
            tier_app.resolve_tier("typo")

    @pytest.mark.parametrize("selection,expected", [
        ("", ["fast", "extract", "vision"]),
        ("  ", ["fast", "extract", "vision"]),
        (" vision, , fast ", ["vision", "fast"]),
    ])
    def test_enabled_tier_selection(self, tier_app, monkeypatch, selection, expected):
        monkeypatch.setenv("MODAL_VLLM_TIERS", selection)
        assert tier_app.enabled_tiers() == expected

    def test_unknown_enabled_tier_is_rejected(self, tier_app, monkeypatch):
        monkeypatch.setenv("MODAL_VLLM_TIERS", "fast,typo")
        with pytest.raises(ValueError, match="unknown tier.*typo"):
            tier_app.enabled_tiers()

    def test_only_selected_server_is_registered(self, tier_app, monkeypatch):
        monkeypatch.setenv("MODAL_VLLM_TIERS", "vision")
        mod = _load_app_module()
        assert not hasattr(mod, "serve_fast")
        assert not hasattr(mod, "serve_extract")
        assert mod.serve_vision._web_label == "mailroom-vllm-vision"
        assert mod.serve_vision._max_inputs == mod.TIERS["vision"].max_inputs
        launch = []
        monkeypatch.setattr(mod, "_launch", launch.append)
        mod.serve_vision()
        assert launch == ["vision"]

    def test_deployment_forwards_only_model_settings_and_credentials(self, tier_app, monkeypatch):
        assert tier_app._config_secrets() == []
        monkeypatch.setenv("MODAL_VLLM_EXTRACT_MODEL", "custom/extract")
        monkeypatch.setenv("MODAL_VLLM_API_TOKEN", "test-token")
        monkeypatch.setenv("HF_TOKEN", "test-hf-token")
        monkeypatch.setenv("MODAL_VLLM_FAST_REVISION", "")
        monkeypatch.setenv("UNRELATED_SECRET", "must-not-forward")
        assert tier_app._config_secrets() == [("secret", {
            "MODAL_VLLM_EXTRACT_MODEL": "custom/extract",
            "MODAL_VLLM_API_TOKEN": "test-token", "HF_TOKEN": "test-hf-token",
        })]

    def test_tier_launch_uses_scoped_config_without_logging_tokens(self, tier_app, monkeypatch, mocker, capsys):
        monkeypatch.setenv("MODAL_VLLM_EXTRACT_MODEL", "custom/extract")
        monkeypatch.setenv("MODAL_VLLM_API_TOKEN", "test-private-token")
        monkeypatch.setenv("HF_TOKEN", "test-private-hf")
        popen = mocker.patch.object(tier_app.subprocess, "Popen")
        tier_app._launch("extract")
        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        assert cmd[:3] == ["vllm", "serve", "custom/extract"]
        assert cmd[cmd.index("--served-model-name") + 1] == "mailroom-extract"
        env = popen.call_args.kwargs["env"]
        assert env["VLLM_API_KEY"] == "test-private-token"
        assert env["HF_TOKEN"] == "test-private-hf"
        output = capsys.readouterr().out
        assert "mailroom-extract" in output
        assert "test-private-token" not in output
        assert "test-private-hf" not in output

    @pytest.mark.parametrize("tier", ["fast", "extract", "vision"])
    def test_registration_applies_tier_resources_and_decorator_order(self, tier_app, monkeypatch, mocker, tier):
        for knob, value in {
            "GPU": "H100:2", "MAX_INPUTS": "9", "MIN_CONTAINERS": "1",
            "MAX_CONTAINERS": "3", "SCALEDOWN_SECONDS": "120",
        }.items():
            monkeypatch.setenv(f"MODAL_VLLM_{tier.upper()}_{knob}", value)
        spec = tier_app.resolve_tier(tier)
        events = []

        def decorator(name):
            def apply(fn):
                events.append(name)
                return fn
            return apply

        web = mocker.patch.object(tier_app.modal, "web_server", return_value=decorator("web"))
        concurrent = mocker.patch.object(tier_app.modal, "concurrent", return_value=decorator("concurrent"))
        function = mocker.patch.object(tier_app.app, "function", return_value=decorator("function"))
        serve = mocker.Mock()
        assert tier_app._tier_function(spec)(serve) is serve
        assert events == ["web", "concurrent", "function"]
        web.assert_called_once_with(
            port=8000, startup_timeout=tier_app.STARTUP_TIMEOUT_SECONDS, label=f"mailroom-vllm-{tier}",
        )
        concurrent.assert_called_once_with(max_inputs=9)
        function.assert_called_once_with(
            gpu="H100:2",
            volumes={tier_app.HF_CACHE_MOUNT: tier_app.hf_cache, tier_app.VLLM_CACHE_MOUNT: tier_app.vllm_cache},
            secrets=tier_app._config_secrets(), timeout=1800, scaledown_window=120,
            min_containers=1, max_containers=3, startup_timeout=tier_app.STARTUP_TIMEOUT_SECONDS,
        )

    @pytest.mark.parametrize("tier,model,revision,expected_model,expected_revision", [
        ("", "", "", "Qwen/Qwen3-8B-FP8", None),
        ("extract", "", "", "custom/extract", "tier-revision"),
        ("extract", "override/model", "explicit-revision", "override/model", "explicit-revision"),
    ])
    def test_download_uses_tier_defaults_and_commits_populated_cache(
        self, tier_app, monkeypatch, mocker, tmp_path, snapshot_download,
        tier, model, revision, expected_model, expected_revision,
    ):
        monkeypatch.setenv("MODAL_VLLM_EXTRACT_MODEL", "custom/extract")
        monkeypatch.setenv("MODAL_VLLM_EXTRACT_REVISION", "tier-revision")
        (tmp_path / "config.json").write_text("{}")
        snapshot_download.return_value = str(tmp_path)
        cache = mocker.Mock()
        monkeypatch.setattr(tier_app, "hf_cache", cache)
        tier_app.download_model(model=model, revision=revision, tier=tier)
        snapshot_download.assert_called_once_with(repo_id=expected_model, revision=expected_revision)
        cache.commit.assert_called_once_with()

    @pytest.mark.parametrize("snapshot", ["empty-directory", "", None])
    def test_empty_download_never_commits_cache(self, tier_app, monkeypatch, mocker, tmp_path, snapshot_download, snapshot):
        snapshot_download.return_value = str(tmp_path) if snapshot == "empty-directory" else snapshot
        cache = mocker.Mock()
        monkeypatch.setattr(tier_app, "hf_cache", cache)
        with pytest.raises(SystemExit, match="empty snapshot"):
            tier_app.download_model(tier="vision")
        snapshot_download.assert_called_once_with(repo_id="Qwen/Qwen3-VL-8B-Instruct-FP8", revision=None)
        cache.commit.assert_not_called()

    def test_failed_download_propagates_without_committing(self, tier_app, monkeypatch, mocker, snapshot_download):
        error = OSError("synthetic download failure")
        snapshot_download.side_effect = error
        cache = mocker.Mock()
        monkeypatch.setattr(tier_app, "hf_cache", cache)
        with pytest.raises(OSError) as caught:
            tier_app.download_model(tier="extract")
        assert caught.value is error
        cache.commit.assert_not_called()
