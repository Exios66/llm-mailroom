"""Per-document-type scorers: tier 0 (common), tier 1 (type) and tier 2 (subclass).

``DOC_SCORERS`` maps each document type that has its own module to its scorer;
``get_doc_scorer`` resolves aliases (specialist agent names, case, whitespace) and
falls back to ``generic``, so every document is scored by something.
"""

from __future__ import annotations

from typing import Any

from ..suites import SPECIALIST_DOC_TYPES
from .base import DocScorer, TypeScorer
from .generic import SCORER as GENERIC

from .contract import SCORER as _CONTRACT
from .corporate_record import SCORER as _CORPORATE
from .correspondence import SCORER as _CORRESPONDENCE
from .insurance_claim import SCORER as _INSURANCE
from .merger_agreement import SCORER as _MERGER

#: Document type -> its scorer. Type modules are added here as they land.
DOC_SCORERS: dict[str, DocScorer] = {
    s.doc_type: s
    for s in (_CONTRACT, _CORPORATE, _CORRESPONDENCE, _INSURANCE, _MERGER)
}


def canonical_doc_type(doc_type: Any) -> str:
    """Lower-case, strip and resolve specialist aliases (``contracts_specialist`` → ``contract``)."""
    key = str(doc_type or "").strip().lower()
    return SPECIALIST_DOC_TYPES.get(key, key)


def get_doc_scorer(doc_type: str | None) -> DocScorer:
    """The scorer for ``doc_type``; specialist names resolve; unknown types get ``generic``."""
    key = str(doc_type or "").strip().lower()
    key = SPECIALIST_DOC_TYPES.get(key, key)
    if key == "generic":
        return GENERIC
    return DOC_SCORERS.get(key, GENERIC)


__all__ = ["DOC_SCORERS", "canonical_doc_type", "DocScorer", "GENERIC", "TypeScorer", "get_doc_scorer"]
