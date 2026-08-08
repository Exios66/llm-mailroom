import pytest

from observability import tracing
from observability import langfuse_setup
from observability import braintrust_setup


@pytest.fixture(autouse=True)
def _clear_observability_env(monkeypatch):
    """Isolate backend selection per test."""
    monkeypatch.delenv("OBSERVABILITY_PROVIDER", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT", raising=False)
    monkeypatch.setattr(langfuse_setup, "_langfuse_client", None)
    monkeypatch.setattr(braintrust_setup, "_configured", False)


class TestProviderResolution:
    def test_defaults_to_none_without_keys(self):
        assert tracing.resolve_provider_name() == "none"
        assert tracing.is_enabled() is False

    def test_auto_prefers_langfuse_when_keys_present(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        assert tracing.resolve_provider_name() == "langfuse"

    def test_auto_uses_braintrust_when_only_braintrust_key(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "bt-test")
        assert tracing.resolve_provider_name() == "braintrust"

    def test_explicit_providers(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "braintrust")
        assert tracing.resolve_provider_name() == "braintrust"
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "langfuse")
        assert tracing.resolve_provider_name() == "langfuse"
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        assert tracing.resolve_provider_name() == "none"

    def test_host_alias_prefers_langfuse_host(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
        assert langfuse_setup._resolve_host() == "https://us.cloud.langfuse.com"
        monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
        assert langfuse_setup._resolve_host() == "http://localhost:3000"


class TestInstrumentation:
    def test_instrument_returns_same_client_when_none(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        client = object()
        assert tracing.instrument_openai_client(client) is client

    def test_instrument_braintrust_unchanged_without_api_key(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "braintrust")
        client = object()
        assert tracing.instrument_openai_client(client) is client

    def test_langfuse_instrument_returns_same_client(self, monkeypatch):
        # No real keys → Langfuse init returns the noop stub, but the original
        # client must still be returned unchanged (no crash, no network).
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "langfuse")
        client = object()
        assert langfuse_setup.instrument_openai_client(client) is client
        assert isinstance(langfuse_setup.get_langfuse_client(), langfuse_setup._NoopLangfuse)


class TestLangfuseClient:
    def test_noop_when_uninitialized(self):
        assert isinstance(langfuse_setup.get_langfuse_client(), langfuse_setup._NoopLangfuse)

    def test_flush_is_safe_without_config(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        tracing.flush()  # must not raise

    def test_noop_methods_chain(self):
        noop = langfuse_setup._NoopLangfuse()
        with noop.start_as_current_observation(as_type="span", name="x") as span:
            span.update(output="ok")
        noop.start_observation(name="y").end()
        noop.update_current_span(output="ok")
        noop.set_current_trace_io(input="i", output="o")
        assert noop.create_trace_id(seed="s") is None
        noop.flush()
        noop.shutdown()

    def test_pipeline_trace_noops_without_config(self):
        with langfuse_setup.pipeline_trace(seed="x", session_id="m1", name="document-pipeline") as root:
            assert root is None
        with langfuse_setup.observation("classify-document") as span:
            assert span is None

    def test_traced_node_runs_without_config(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        from observability.tracing import traced_node

        calls = []

        @traced_node("classify-document")
        def node(state):
            calls.append(state)
            return {"stage": "classified"}

        assert node({"doc_id": "1"}) == {"stage": "classified"}
        assert calls == [{"doc_id": "1"}]


class TestLangfuseCallAttrs:
    def test_empty_when_not_langfuse(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        from observability.tracing import langfuse_call_attrs

        assert langfuse_call_attrs("sorter") == {}

    def test_name_when_langfuse(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "langfuse")
        from observability.tracing import langfuse_call_attrs

        attrs = langfuse_call_attrs("contracts_specialist")
        assert attrs["name"] == "contracts_specialist"
        assert "metadata" not in attrs

        with_meta = langfuse_call_attrs("reporter", metadata={"doc_id": "x"})
        assert with_meta["metadata"] == {"doc_id": "x"}
