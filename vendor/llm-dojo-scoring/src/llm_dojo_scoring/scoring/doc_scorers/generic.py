"""``generic``: the scorer for types with no module of their own.

Covers the native classes with no corpus rows (``due_diligence``,
``compliance_filing``, ``court_opinion``) and any unknown type: tier 0 only.
"""

from __future__ import annotations

from .base import TypeScorer

SCORER = TypeScorer(doc_type="generic")
