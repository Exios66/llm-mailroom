"""``contract``: CUAD clause detection replaces the headline when CUAD labels exist.

Contract ground truth from CUAD is clause spans only, so the field map never meets it;
the headline is CUAD category-detection F1 within the row's labeled universe (SAND-032).
Tier 2 normalises folder-style CUAD surfaces (``License_Agreements``) to families.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...pipeline.sandbox_scoring import score_cuad
from ...schemas.corpus import DOC_TYPE_SUBCLASSES
from .base import TypeScorer


class ContractScorer(TypeScorer):
    """CUAD extras and headline."""

    def extras(
        self,
        predicted: Mapping[str, Any],
        raw_expected: Mapping[str, Any],
        *,
        subclass: str | None,
        doc_text: str | None,
    ) -> dict[str, Any]:
        """CUAD presence scores when the row carries ``cuad_clause_labels``."""
        labels = raw_expected.get("cuad_clause_labels")
        if not labels:
            return {}
        scores = dict(score_cuad(predicted, labels))
        # An empty label map (e.g. the string "{}") is no benchmark: keep the field score.
        return scores if scores.get("cuad_presence_f1") is not None else {}

    def headline(self, common: Mapping[str, Any], extras: Mapping[str, Any]) -> tuple[float | None, str | None]:
        """``cuad_presence_f1`` whenever CUAD was scored."""
        if "cuad_presence_f1" in extras:
            return extras["cuad_presence_f1"], "cuad_presence_f1"
        return None, None


SCORER = ContractScorer(doc_type="contract", subclasses=DOC_TYPE_SUBCLASSES["contract"])
