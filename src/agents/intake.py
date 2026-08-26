"""Deterministic intake clerk — whitespace / hyphen / NBSP normalize.

Procedural (not an LLM agent). The clerk gold lives in llm-dojo-scoring
(``llm_dojo_scoring.intake``); this module is a thin wrapper that re-exports
the byte-compatible primitives and emits the live ``normalize-intake`` span
that The-Mailroom reads. Dojo's own ``apply_intake`` is span-less (``filename``
is discarded there) — mailroom owns tracing.

The-Mailroom still mirrors ``deterministic_normalize`` / ``looks_messy`` in
``mailroom_ui/intake_normalize.py`` so the eval harness can exercise the same
rules without importing this repo. Do not invent a third algorithm here.
"""

from __future__ import annotations

from llm_dojo_scoring.intake import (
    INTAKE_LIVE_METHOD,
    INTAKE_PREP_STEPS,
    INTAKE_SPAN,
    INTAKE_SPAN_KEYS,
    deterministic_normalize,
    intake_span_output,
    looks_messy,
)

__all__ = [
    "INTAKE_LIVE_METHOD",
    "INTAKE_PREP_STEPS",
    "INTAKE_SPAN",
    "INTAKE_SPAN_KEYS",
    "apply_intake",
    "deterministic_normalize",
    "intake_span_output",
    "looks_messy",
]


def apply_intake(text: str, *, filename: str | None = None) -> tuple[str, dict]:
    """Normalize ``text`` and emit the ``normalize-intake`` span.

    Returns ``(cleaned_text, stats)`` where ``stats`` includes the span
    payload keys (``messy``, ``method``, ``chars``) plus the raw normalize
    counters. Tracing no-ops when Langfuse is not the active backend.
    """
    from observability.tracing import observation

    cleaned, stats = deterministic_normalize(text)
    messy = looks_messy(cleaned, stats)
    payload = intake_span_output(stats, messy, method=INTAKE_LIVE_METHOD)
    with observation(
        INTAKE_SPAN,
        as_type="span",
        input={"file": filename, "raw_chars": stats.get("raw_chars", 0)},
    ) as span:
        if span is not None:
            span.update(output=payload)
    return cleaned, {**stats, **payload}
