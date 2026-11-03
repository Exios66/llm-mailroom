"""``merger_agreement``: MAUD answers replace the headline when MAUD labels exist.

Merger ground truth from MAUD is question-to-answer labels only, so the field map never
meets it; the headline is MAUD accuracy (SAND-032). Tier 2 normalises the MAUD
consideration type.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...pipeline.sandbox_scoring import score_maud
from ...schemas.corpus import DOC_TYPE_SUBCLASSES
from .base import TypeScorer


class MergerAgreementScorer(TypeScorer):
    """MAUD extras and headline."""

    def extras(
        self,
        predicted: Mapping[str, Any],
        raw_expected: Mapping[str, Any],
        *,
        subclass: str | None,
        doc_text: str | None,
    ) -> dict[str, Any]:
        """MAUD answer scores when the row carries ``maud_clause_labels``."""
        labels = raw_expected.get("maud_clause_labels")
        if not labels:
            return {}
        scores = dict(score_maud(predicted, labels))
        # An empty label map (e.g. the string "{}") is no benchmark: keep the field score.
        return scores if scores.get("maud_accuracy") is not None else {}

    def headline(self, common: Mapping[str, Any], extras: Mapping[str, Any]) -> tuple[float | None, str | None]:
        """``maud_accuracy`` whenever MAUD was scored."""
        if "maud_accuracy" in extras:
            return extras["maud_accuracy"], "maud_accuracy"
        return None, None


SCORER = MergerAgreementScorer(doc_type="merger_agreement", subclasses=DOC_TYPE_SUBCLASSES["merger_agreement"])
