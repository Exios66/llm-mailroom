"""``insurance_claim``: claims consistency extras (tier 1) and the CMS source table (tier 2).

``determination_consistency`` and ``amount_exactness`` are computed by the insurance
suite only for callers of ``suite.score`` itself; tier 0 drops them. This module
reports them on the document payload; they never feed ``overall_extraction_score``.
Tier 2 derives ``cms_source_table`` from a CMS row's subclass, because the published
ground truth never carries the key (:meth:`InsuranceClaimScorer.derived_fields`).
"""

from __future__ import annotations

from typing import Any, Mapping

from ...schemas.corpus import DOC_TYPE_SUBCLASSES, cms_source_table
from ..claims_consistency import score_claims_extras
from ..extraction_scope import scope_extraction_pair
from .base import TypeScorer


class InsuranceClaimScorer(TypeScorer):
    """Insurance claims: consistency extras and the derived CMS table."""

    def extras(
        self,
        predicted: Mapping[str, Any],
        raw_expected: Mapping[str, Any],
        *,
        subclass: str | None,
        doc_text: str | None,
    ) -> dict[str, Any]:
        """Claims consistency on the scoped pair the suite sees; ``{}`` with no ground truth."""
        _, expected = scope_extraction_pair(self.doc_type, dict(predicted), dict(raw_expected))
        if not expected:
            return {}
        return dict(score_claims_extras(expected, predicted))

    def derived_fields(self, raw_subclass: Any) -> dict[str, str]:
        """``{"cms_source_table": table}`` for CMS rows, else ``{}``."""
        table = cms_source_table(self.doc_type, raw_subclass)
        return {"cms_source_table": table} if table else {}


SCORER = InsuranceClaimScorer(doc_type="insurance_claim", subclasses=DOC_TYPE_SUBCLASSES["insurance_claim"])
