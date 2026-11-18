"""Registry and handoff contract for the closed-field resolver (decision slot ``closed_field_resolver``).

A resolver is a plain callable, ``resolver(doc_text, handoff) -> Mapping``. It is asked
about ONE closed-label field whose extracted value is off its taxonomy vocabulary
(``pipeline.label_vocab.out_of_vocabulary_fields``) and answers with the allowed value it
picks (``value``) and that value's probability (``probability``, 0-1), optionally the
``model`` that answered. As with the extraction gate, the graph never constructs one: the
host (the sandbox, an eval, a test) registers it. The experiments run it on an OpenRouter
Decisions-API model, which answers a pick-one question over ``handoff["allowed"]`` with a
probability per option; the mailroom does not know or care which service answered.

The handoff carries the class extracted for, the sorter's subclass (when it gave one),
the field, the off-list value and the allowed values in taxonomy order. It never carries
ground truth.

With nothing registered, or with ``decision_slots.closed_field_resolver.mode: baseline``
(the default), an off-list value parks the document exactly as #285 made it do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

ClosedFieldResolver = Callable[[str, dict[str, Any]], Mapping[str, Any]]

_resolver: ClosedFieldResolver | None = None


def register_closed_field_resolver(resolver: ClosedFieldResolver | None) -> None:
    """Install ``resolver`` process-wide; ``None`` removes it."""
    global _resolver
    _resolver = resolver


def get_closed_field_resolver() -> ClosedFieldResolver | None:
    """Return the registered process-wide resolver, or None if absent."""
    return _resolver


def closed_field_handoff(
    doc_type: str, field: str, value: Any, allowed: tuple[str, ...] | list[str], doc_subclass: str | None = None
) -> dict[str, Any]:
    """What the resolver sees for one off-list field (a fresh dict; editing it changes nothing)."""
    out: dict[str, Any] = {
        "doc_type": doc_type,
        "field": field,
        "value": value if isinstance(value, (str, int, float, bool)) or value is None else str(value),
        "allowed": list(allowed),
    }
    if isinstance(doc_subclass, str) and doc_subclass.strip():
        out["doc_subclass"] = doc_subclass.strip()
    return out


__all__ = [
    "ClosedFieldResolver",
    "closed_field_handoff",
    "get_closed_field_resolver",
    "register_closed_field_resolver",
]
