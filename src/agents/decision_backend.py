"""Registry for the decision-model reviewer backend.

A backend is a plain callable, ``backend(doc_text, doc_types,
subclass_catalogs) -> DecisionAnswer``. The graph never constructs one: the
host (the sandbox, an eval, a test) registers it. With nothing registered,
``decision_only`` classes park for a person; the default config never reads
this registry.
"""

from __future__ import annotations

from typing import Callable, Optional

from pipeline.decision_review import DecisionAnswer

DecisionBackend = Callable[[str, list[str], dict[str, list[str]]], DecisionAnswer]

_backend: Optional[DecisionBackend] = None


def register_decision_backend(backend: Optional[DecisionBackend]) -> None:
    """Install ``backend`` process-wide; ``None`` removes it."""
    global _backend
    _backend = backend


def get_decision_backend() -> Optional[DecisionBackend]:
    return _backend
