"""Registry and handoff contract for the extraction-gate reviewer.

A gate is a plain callable, ``gate(doc_text, handoff) -> Mapping``. It reviews one
specialist extraction and answers with at least a ``verdict``. The Decisions-model
gate also returns ``key_field_score`` and ``field_support``. As with the decision
backend, the graph never constructs a gate: the host (the sandbox, an eval, a test)
registers one. With nothing registered, extraction runs exactly as before.

The handoff carries the **sorter's** subclass (``doc_subclass``). Gate question
sets 1.3.0+ pick per-template field rules by subclass, so a wrong sorter subclass
gives the gate the wrong rules, and with no subclass the gate falls back to its
class-level (1.2.0) rules. Never fill it from a ground-truth label: the gate was
calibrated with labelled subclasses, and pipeline gate numbers are those numbers
reduced by the sorter's own subclass error rate.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

ExtractionGate = Callable[[str, dict[str, Any]], Mapping[str, Any]]

_gate: ExtractionGate | None = None


def register_extraction_gate(gate: ExtractionGate | None) -> None:
    """Install ``gate`` process-wide; ``None`` removes it."""
    global _gate
    _gate = gate


def get_extraction_gate() -> ExtractionGate | None:
    """Return the registered process-wide gate, or None if absent."""
    return _gate


def extraction_gate_handoff(
    doc_type: str, extraction: Mapping[str, Any] | None, doc_subclass: str | None = None
) -> dict[str, Any]:
    """What the gate sees: the class extracted for, the sorter's subclass and the fields.

    Underscore keys (parse diagnostics, raw text) and ``confidence`` are dropped.
    ``doc_subclass`` is included only when the sorter gave one. The fields are a deep
    copy, so a gate that edits its handoff can never change the extraction.
    """
    fields = copy.deepcopy({
        k: v for k, v in (extraction or {}).items()
        if not str(k).startswith("_") and k != "confidence"
    })
    out: dict[str, Any] = (
        {"doc_type": doc_type, "fields": fields} if fields else {"doc_type": doc_type, "available": False}
    )
    if isinstance(doc_subclass, str) and doc_subclass.strip():
        out["doc_subclass"] = doc_subclass.strip()
    return out


__all__ = [
    "ExtractionGate",
    "extraction_gate_handoff",
    "get_extraction_gate",
    "register_extraction_gate",
]
