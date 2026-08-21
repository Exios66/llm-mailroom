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
        def from_local(*names):
            return ("secret", names)

    class _Volume:
        @staticmethod
        def from_name(name, create_if_missing=False):
            return ("volume", name)

    class _Image:
        @staticmethod
        def from_registry(ref, add_python=None):
            return _Image()

        def run_commands(self, *cmds):
            return self

        def env(self, mapping):
            return self

    class _App:
        def __init__(self, name, image=None):
            self.name = name

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

    def _web_server(port=None, startup_timeout=None):
        def deco(fn):
            return fn

        return deco

    stub.Secret = _Secret
    stub.Volume = _Volume
    stub.Image = _Image
    stub.App = _App
    stub.web_server = _web_server
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
