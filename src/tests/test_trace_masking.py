"""Export-stage span masking (plan Task 18, Review Focus 2)."""

from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

from observability import masking


def _span(name, attrs, scope="openai", sid="1"):
    return OtelSpanData(
        trace_id="a" * 32,
        span_id=sid.rjust(16, "0"),
        parent_span_id=None,
        name=name,
        instrumentation_scope_name=scope,
        instrumentation_scope_version=None,
        attributes=attrs,
        resource_attributes={},
    )


def _run(*spans):
    params = MaskOtelSpansParams(
        spans={OtelSpanIdentifier(s.trace_id, s.span_id): s for s in spans}
    )
    res = masking.mask_otel_spans(params=params)
    return res, params


def test_emails_redacted_in_generation_input():
    s = _span("classify", {"langfuse.observation.input": "From: alice@example.com hi"})
    res, params = _run(s)
    ident = next(iter(params.spans))
    patch = res.span_patches[ident]
    assert patch.set_attributes["langfuse.observation.input"] == "From: [email] hi"


def test_pipeline_result_judge_input_untouched():
    s = _span("pipeline-result", {"langfuse.observation.input": "alice@example.com"}, scope="langfuse")
    assert _run(s)[0] is None


def test_clean_batch_returns_none():
    assert _run(_span("x", {"a": "no pii", "n": 3}))[0] is None


def test_prompt_bodies_dropped_only_when_redact_flag_set(monkeypatch):
    attrs = {"gen_ai.prompt.0.content": "secret", "gen_ai.completion.0.content": "secret2", "gen_ai.request.model": "m"}
    monkeypatch.delenv("MAILROOM_TRACE_REDACT", raising=False)
    assert _run(_span("g", attrs))[0] is None
    monkeypatch.setenv("MAILROOM_TRACE_REDACT", "1")
    res, params = _run(_span("g", attrs))
    patch = res.span_patches[next(iter(params.spans))]
    assert set(patch.delete_attributes) == {"gen_ai.prompt.0.content", "gen_ai.completion.0.content"}
    assert "gen_ai.request.model" not in patch.delete_attributes


def test_redact_flag_never_strips_pipeline_result(monkeypatch):
    monkeypatch.setenv("MAILROOM_TRACE_REDACT", "1")
    s = _span("pipeline-result", {"gen_ai.prompt.0.content": "judge input"}, scope="langfuse")
    assert _run(s)[0] is None


def test_intake_meta_allowlist_drops_sender():
    meta = {
        "source": "gmail",
        "route": "triage",
        "message_id": "<abc@mail.gmail.com>",
        "sender": "alice@example.com",
        "subject": "private",
    }
    out = masking.allowlist_intake_meta(meta)
    assert set(out) == {"source", "route", "message_id_hash"}
    assert "alice" not in repr(out) and "abc@" not in repr(out)
    assert len(out["message_id_hash"]) == 16


def test_client_kwargs_wires_masking():
    from observability.langfuse_setup import client_kwargs

    assert client_kwargs()["mask_otel_spans"] is masking.mask_otel_spans


def test_triage_trace_metadata_uses_allowlist():
    from pathlib import Path
    from pipeline import watcher

    kw = watcher._triage_trace_kwargs(Path("a.txt"), {"source": "gmail", "sender": "alice@example.com"})
    assert "sender" not in kw["metadata"]
