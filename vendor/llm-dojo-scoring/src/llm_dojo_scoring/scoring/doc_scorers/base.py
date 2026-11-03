"""The ``DocScorer`` protocol and a base class for per-document-type scorers.

Tier 1 (``extras`` and ``headline``) and tier 2 (``subclass``) live in one module per
document type. Each module defines a ``TypeScorer`` instance and the registry in
``doc_scorers/__init__.py`` collects them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from ...schemas.corpus import normalize_corpus_subclass


@runtime_checkable
class DocScorer(Protocol):
    """What the dispatcher needs from a document type's scorer."""

    doc_type: str
    subclasses: tuple[str, ...]
    #: Tier-0 suite path: ``True`` forces the detailed path, ``None`` keeps the default rule.
    detailed: bool | None

    def subclass(self, raw: Any) -> str | None:
        """Tier 2: the canonical subclass key for a raw value, or ``None``."""

    def extras(
        self,
        predicted: Mapping[str, Any],
        raw_expected: Mapping[str, Any],
        *,
        subclass: str | None,
        doc_text: str | None,
    ) -> dict[str, Any]:
        """Tier 1: type-specific scores added to the payload."""

    def headline(self, common: Mapping[str, Any], extras: Mapping[str, Any]) -> tuple[float | None, str | None]:
        """Tier 1: ``(score, metric_name)`` replacing the headline, or ``(None, None)``."""


@dataclass(frozen=True)
class TypeScorer:
    """Default per-type scorer: subclass normalisation, no extras, no headline swap."""

    doc_type: str
    subclasses: tuple[str, ...] = ()
    detailed: bool | None = None

    def subclass(self, raw: Any) -> str | None:
        """Normalise ``raw`` through the corpus catalogue; ``None`` for types without one."""
        if not self.subclasses or raw is None or not str(raw).strip():
            return None
        return normalize_corpus_subclass(self.doc_type, raw)

    def extras(
        self,
        predicted: Mapping[str, Any],
        raw_expected: Mapping[str, Any],
        *,
        subclass: str | None,
        doc_text: str | None,
    ) -> dict[str, Any]:
        """No type-specific extras."""
        return {}

    def headline(self, common: Mapping[str, Any], extras: Mapping[str, Any]) -> tuple[float | None, str | None]:
        """No headline swap."""
        return None, None
