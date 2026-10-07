"""Export-stage PII masking for Langfuse spans (langfuse >= 4.6 ``mask_otel_spans``).

Runs on the OpenTelemetry export worker thread, so it is pure, fast and
deterministic. Two layers:

* e-mail addresses are redacted from every string span attribute;
* with ``MAILROOM_TRACE_REDACT=1`` generation prompt/completion bodies are
  dropped entirely.

The ``pipeline-result`` generation is the judge/evaluator target — its input
carries the evaluation signal — so it is NEVER touched by either layer.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PROTECTED_SPAN_NAMES = frozenset({"pipeline-result"})
_BODY_PREFIXES = ("gen_ai.prompt", "gen_ai.completion", "llm.input_messages", "llm.output_messages")
_OBS_BODY_KEYS = ("langfuse.observation.input", "langfuse.observation.output")
INTAKE_META_ALLOWLIST = ("source", "route")


def redact_emails(text: str) -> str:
    return EMAIL_RE.sub("[email]", text)


def _redact_enabled() -> bool:
    return os.environ.get("MAILROOM_TRACE_REDACT", "0") == "1"


def mask_otel_spans(*, params: MaskOtelSpansParams) -> Optional[MaskOtelSpansResult]:
    redact_bodies = _redact_enabled()
    patches = {}
    for identifier, span in params.spans.items():
        if span.name in PROTECTED_SPAN_NAMES:
            continue
        set_attrs: dict = {}
        delete_attrs: list = []
        is_generation = span.attributes.get("langfuse.observation.type") == "generation"
        for key, value in span.attributes.items():
            if redact_bodies and (
                key.startswith(_BODY_PREFIXES) or (is_generation and key in _OBS_BODY_KEYS)
            ):
                delete_attrs.append(key)
            elif isinstance(value, str):
                redacted = redact_emails(value)
                if redacted != value:
                    set_attrs[key] = redacted
        if set_attrs or delete_attrs:
            patches[identifier] = OtelSpanPatch(
                set_attributes=set_attrs, delete_attributes=tuple(delete_attrs)
            )
    return MaskOtelSpansResult(span_patches=patches) if patches else None


def allowlist_intake_meta(meta: dict | None) -> dict:
    """Trace-safe subset of the Gmail intake sidecar (no sender/subject/body)."""
    meta = meta or {}
    out = {k: meta[k] for k in INTAKE_META_ALLOWLIST if k in meta}
    message_id = meta.get("message_id")
    if message_id:
        out["message_id_hash"] = hashlib.sha256(str(message_id).encode()).hexdigest()[:16]
    return out
