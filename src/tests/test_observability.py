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
    monkeypatch.delenv("LANGFUSE_FLUSH_AT", raising=False)
    monkeypatch.delenv("LANGFUSE_FLUSH_INTERVAL", raising=False)
    monkeypatch.delenv("LANGFUSE_RELEASE", raising=False)
    monkeypatch.delenv("LANGFUSE_TIMEOUT", raising=False)
    monkeypatch.delenv("LANGFUSE_SAMPLE_RATE", raising=False)
    monkeypatch.delenv("LANGFUSE_TRACING_ENVIRONMENT", raising=False)
    monkeypatch.delenv("OBSERVABILITY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("MAILROOM_TRACE_USER_ID", raising=False)
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT", raising=False)
    monkeypatch.delenv("PHOENIX_TRACING", raising=False)
    monkeypatch.delenv("PHOENIX_ENDPOINT", raising=False)
    monkeypatch.delenv("PHOENIX_SERVICE_NAME", raising=False)
    monkeypatch.delenv("PHOENIX_PROJECT", raising=False)
    monkeypatch.setattr(langfuse_setup, "_langfuse_client", None)
    monkeypatch.setattr(braintrust_setup, "_configured", False)


class TestProviderResolution:
    def test_defaults_to_phoenix_without_keys(self):
        # Local-first fallback: no cloud keys set -> the cost-free local Phoenix
        # backend (default enabled), not silence.
        assert tracing.resolve_provider_name() == "phoenix"

    def test_phoenix_disabled_without_keys_means_none(self, monkeypatch):
        monkeypatch.setenv("PHOENIX_TRACING", "disabled")
        assert tracing.resolve_provider_name() == "none"

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
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "phoenix")
        assert tracing.resolve_provider_name() == "phoenix"
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


class TestFlushHealth:
    def test_flush_health_reports_counters(self, monkeypatch):
        import observability.tracing as tr

        monkeypatch.setattr(tr, "resolve_provider_name", lambda: "none")
        tr._flush_ok = 3
        tr._flush_failures = 0
        h = tr.flush_health()
        assert h["provider"] == "none"
        assert h["flush_ok"] == 3
        assert h["healthy"] is True

    def test_flush_failures_mark_unhealthy(self, monkeypatch):
        import observability.tracing as tr

        monkeypatch.setattr(tr, "resolve_provider_name", lambda: "langfuse")

        def boom():
            raise RuntimeError("backend down")

        monkeypatch.setattr("observability.langfuse_setup.flush_langfuse", boom)
        tr.flush()
        assert tr._flush_failures >= 1
        assert tr.flush_health()["healthy"] is False


class TestLogContextvars:
    def test_run_pipeline_binds_and_unbinds(self, temp_base_dir, mock_openai_client, mock_langchain_llm):
        import structlog
        from graph.build_graph import run_pipeline
        from pathlib import Path

        src = Path(temp_base_dir) / "inbox" / "ctx.txt"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("Service agreement between Acme and Beta for one year.")

        with structlog.contextvars.bound_contextvars():
            result = run_pipeline(src, "MATTER-CTX")
            assert result.get("stage") in ("archived", "review", "failed")


class TestLangfuseClientKwargs:
    def test_batching_and_environment_from_env(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_FLUSH_AT", "64")
        monkeypatch.setenv("LANGFUSE_FLUSH_INTERVAL", "2.5")
        monkeypatch.setenv("OBSERVABILITY_ENVIRONMENT", "misc")
        monkeypatch.setenv("LANGFUSE_RELEASE", "mailroom@test")
        monkeypatch.setenv("LANGFUSE_TIMEOUT", "12")
        monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0.5")
        kwargs = langfuse_setup.client_kwargs()
        assert kwargs["flush_at"] == 64
        assert kwargs["flush_interval"] == 2.5
        assert kwargs["environment"] == "misc"
        assert kwargs["release"] == "mailroom@test"
        assert kwargs["timeout"] == 12
        assert kwargs["sample_rate"] == 0.5

    def test_defaults_omit_flush_overrides(self):
        kwargs = langfuse_setup.client_kwargs()
        assert "flush_at" not in kwargs
        assert "flush_interval" not in kwargs
        assert "environment" not in kwargs
        assert kwargs["release"]
        assert kwargs["host"]

    def test_invalid_batching_env_is_omitted(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_FLUSH_AT", "not-an-int")
        monkeypatch.setenv("LANGFUSE_FLUSH_INTERVAL", "nope")
        kwargs = langfuse_setup.client_kwargs()
        assert "flush_at" not in kwargs
        assert "flush_interval" not in kwargs

    def test_tracing_environment_alias(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "pilot")
        assert langfuse_setup.client_kwargs()["environment"] == "pilot"


class TestObservationTypes:
    def test_data_model_types_for_pipeline_nodes(self):
        assert tracing.observation_type_for("document-pipeline") == "chain"
        assert tracing.observation_type_for("classify-document") == "agent"
        assert tracing.observation_type_for("extract-fields") == "agent"
        assert tracing.observation_type_for("compile-report") == "agent"
        assert tracing.observation_type_for("arbitrate-verdict") == "agent"
        assert tracing.observation_type_for("adjudicate-conflict") == "agent"
        assert tracing.observation_type_for("judge-verify") == "evaluator"
        assert tracing.observation_type_for("transcribe-pdf") == "retriever"
        assert tracing.observation_type_for("extract-image-text") == "retriever"
        assert tracing.observation_type_for("pipeline-result") == "generation"
        assert tracing.observation_type_for("answer-question") == "generation"
        assert tracing.observation_type_for("unknown-step") == "span"

    def test_every_graph_traced_node_is_typed(self):
        from pathlib import Path
        import re

        src = Path(__file__).resolve().parents[1] / "graph" / "build_graph.py"
        names = re.findall(r'traced_node\("([^"]+)"', src.read_text())
        assert names, "expected traced_node registrations in build_graph.py"
        missing = [n for n in names if n not in tracing.NODE_OBSERVATION_TYPES]
        assert missing == []

    def test_pipeline_trace_defaults_to_chain(self):
        import inspect

        params = inspect.signature(langfuse_setup.pipeline_trace).parameters
        assert params["as_type"].default == "chain"
        assert params["user_id"].default is None

    def test_traced_node_passes_agent_type(self, monkeypatch):
        from contextlib import contextmanager
        from unittest.mock import MagicMock

        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "langfuse")
        captured = []

        @contextmanager
        def fake_obs(name, **kwargs):
            captured.append((name, kwargs.get("as_type")))
            yield MagicMock()

        monkeypatch.setattr("observability.langfuse_setup.observation", fake_obs)
        deco = tracing.traced_node("classify-document")

        @deco
        def node(state):
            return {"stage": "classified"}

        assert node({"doc_id": "1"}) == {"stage": "classified"}
        assert captured == [("classify-document", "agent")]

    def test_ensure_process_tracing_is_safe_when_disabled(self, monkeypatch):
        monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
        tracing.ensure_process_tracing()  # must not raise

    def test_legalbench_question_name_is_stable(self, monkeypatch):
        from contextlib import contextmanager
        from legalbench.langfuse_tracing import question_observation

        captured = []

        @contextmanager
        def fake_obs(name, **kwargs):
            captured.append({"name": name, **kwargs})
            yield None

        monkeypatch.setattr("legalbench.langfuse_tracing.tracing.observation", fake_obs)
        with question_observation(7, "contract_qa", input_data={"q": "x"}):
            pass
        assert len(captured) == 1
        assert captured[0]["name"] == "answer-question"
        assert captured[0]["as_type"] == "generation"
        assert captured[0]["metadata"]["index"] == 7
        assert captured[0]["metadata"]["task_id"] == "contract_qa"
        assert "q7" not in captured[0]["name"]
