"""DMR-076 — llamafile + ollama provider-seam tests.

Network-free by construction (same discipline as test_vllm_modal_capability):
provider-seam tests exercise llm/providers.py + llm/client.py directly with
monkeypatched env — no HTTP, no server, no API keys. The llamafile/ollama
served-model remap and the keyless ``not-needed`` api_key parity are the
contracts under test.

Covered contracts:
- llamafile ProviderConfig: LLAMAFILE_BASE_URL default + override, keyless
  (api_key_env=None) → get_llm api_key is the "not-needed" placeholder;
- DEFAULT_PROVIDER=llamafile/ollama wins over the per-agent provider;
- champion slugs remap via taxonomy llamafile_model_map / ollama_model_map
  (qwen/qwen3.7-flash → qwen3:7b; deepseek/* → provider-specific tags);
- free-only guardrail exempts ollama/llamafile exactly like vllm;
- openrouter stays the untouched primary path.
"""

from __future__ import annotations

import llm.client as client_mod
import llm.providers as providers_mod


def _fresh_providers():
    providers_mod._providers_cache = None  # rebuild from current env
    return providers_mod


def test_llamafile_provider_defaults(monkeypatch):
    providers = _fresh_providers()
    monkeypatch.delenv("LLAMAFILE_BASE_URL", raising=False)
    provider, model = providers.resolve_provider(
        {"provider": "llamafile", "model": "qwen3:7b"}
    )
    assert provider.name == "llamafile"
    assert provider.api_key_env is None  # keyless, like ollama
    assert provider.base_url == "http://localhost:8080/v1"
    assert model == "qwen3:7b"


def test_llamafile_base_url_env_override(monkeypatch):
    providers = _fresh_providers()
    monkeypatch.setenv("LLAMAFILE_BASE_URL", "http://llamafile:8080/v1")
    provider, _ = providers.resolve_provider({"provider": "llamafile"})
    assert provider.base_url == "http://llamafile:8080/v1"


def test_default_provider_llamafile_wins_over_agent_config(monkeypatch):
    providers = _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "llamafile")
    provider, _ = providers.resolve_provider(
        {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}
    )
    assert provider.name == "llamafile"


def test_get_llm_end_to_end_on_llamafile(monkeypatch):
    """Keyless parity: api_key_env=None must yield the "not-needed"
    placeholder (the same convention the vLLM keyless path uses)."""
    _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "llamafile")
    monkeypatch.setenv(
        "LLAMAFILE_BASE_URL", "http://llamafile:8080/v1"
    )
    monkeypatch.setattr(
        client_mod, "get_agent_config", lambda name: {"provider": "llamafile", "model": "qwen3:7b"}
    )
    monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
    got, model = client_mod.get_llm("sorter")
    # the OpenAI SDK normalizes base_url with a trailing slash — assert with
    # startswith like the vLLM seam tests
    assert str(got.base_url).startswith("http://llamafile:8080/v1")
    assert got.api_key == "not-needed"
    assert model == "qwen3:7b"


def test_get_llm_end_to_end_on_ollama(monkeypatch):
    """Ollama keyless parity + remap (champion → ollama tag)."""
    _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setattr(
        client_mod, "get_agent_config", lambda name: {"provider": "ollama", "model": "qwen/qwen3.7-flash"}
    )
    monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
    got, model = client_mod.get_llm("sorter")
    assert str(got.base_url).startswith("http://ollama:11434/v1")
    assert got.api_key == "not-needed"
    assert model == "qwen3:7b"  # ollama_model_map remap


def test_llamafile_champion_ids_remap_to_aliased_ids(monkeypatch):
    """DMR-076: the taxonomy's OpenRouter slug becomes the served alias."""
    _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "llamafile")
    monkeypatch.setattr(
        client_mod, "get_agent_config", lambda name: {"provider": "llamafile", "model": "qwen/qwen3.7-flash"}
    )
    monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
    _, model = client_mod.get_llm("sorter")
    assert model == "qwen3:7b"


def test_llamafile_remap_deepseek_champions(monkeypatch):
    """All deepseek champions collapse onto the single served GGUF alias."""
    _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "llamafile")
    for champion, served in (
        ("deepseek/deepseek-v4-flash", "qwen3:7b"),
        ("deepseek/deepseek-v4-pro", "qwen3:14b"),
        ("openrouter/free", "qwen3:7b"),
    ):
        monkeypatch.setattr(
            client_mod, "get_agent_config", lambda name, c=champion: {"provider": "llamafile", "model": c}
        )
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        _, model = client_mod.get_llm("sorter")
        assert model == served


def test_ollama_remap_deepseek_champions(monkeypatch):
    """Ollama serves its own tags: r1 8b / 14b for the deepseek champions."""
    _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "ollama")
    for champion, served in (
        ("deepseek/deepseek-v4-flash", "deepseek-r1:8b"),
        ("deepseek/deepseek-v4-pro", "deepseek-r1:14b"),
        ("openrouter/free", "qwen3:7b"),
    ):
        monkeypatch.setattr(
            client_mod, "get_agent_config", lambda name, c=champion: {"provider": "ollama", "model": c}
        )
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        _, model = client_mod.get_llm("sorter")
        assert model == served


def test_unmapped_champion_passes_through(monkeypatch):
    """A champion without a map entry is passed to the server untouched
    (same contract as vllm_model_map; DMR-052)."""
    _fresh_providers()
    monkeypatch.setenv("DEFAULT_PROVIDER", "llamafile")
    monkeypatch.setattr(
        client_mod, "get_agent_config", lambda name: {"provider": "llamafile", "model": "not/in-any-map"}
    )
    monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
    _, model = client_mod.get_llm("sorter")
    assert model == "not/in-any-map"


def test_free_only_guardrail_exempts_llamafile_and_ollama(monkeypatch):
    """DMR-076: like vLLM, local servers have no per-token price — the
    MAILROOM_LLM_FREE_ONLY guardrail must not block them."""
    _fresh_providers()
    monkeypatch.setenv("MAILROOM_LLM_FREE_ONLY", "1")
    for provider, base_env, url in (
        ("llamafile", "LLAMAFILE_BASE_URL", "http://llamafile:8080/v1"),
        ("ollama", "OLLAMA_BASE_URL", "http://ollama:11434/v1"),
    ):
        _fresh_providers()  # providers cache is a module-level singleton — rebuild per iteration
        monkeypatch.setenv("DEFAULT_PROVIDER", provider)
        monkeypatch.setenv(base_env, url)
        monkeypatch.setattr(
            client_mod,
            "get_agent_config",
            lambda name: {"provider": provider, "model": "deepseek/deepseek-v4-pro"},
        )
        monkeypatch.setattr(client_mod, "instrument_client", lambda c: c)
        got, _ = client_mod.get_llm("sorter")  # must not raise
        assert str(got.base_url).startswith(url)


def test_openrouter_primary_unchanged(monkeypatch):
    """The capability must not move the default serving path."""
    _fresh_providers()
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    provider, model = providers_mod.resolve_provider(
        {"provider": "openrouter", "model": "qwen/qwen3.7-flash"}
    )
    assert provider.name == "openrouter"
    assert model == "qwen/qwen3.7-flash"


def test_is_free_only_exempt_set_matches_native_and_vendored_uses():
    """The exemption set is the single source of truth for both paths."""
    assert client_mod.is_free_only_exempt("llamafile")
    assert client_mod.is_free_only_exempt("ollama")
    assert client_mod.is_free_only_exempt("vllm")
    assert client_mod.is_free_only_exempt("generic")
    assert not client_mod.is_free_only_exempt("openrouter")