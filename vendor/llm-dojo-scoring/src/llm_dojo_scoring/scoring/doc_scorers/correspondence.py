"""``correspondence``: Enron topic and sentiment extras (tier 1) and subclass normalisation (tier 2).

The correspondence suite computes topic and sentiment too, but only for callers of
``suite.score`` itself; tier 0 keeps the field-map result and drops them. This module
reports them on the document payload. They never feed ``overall_extraction_score``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...schemas.corpus import DOC_TYPE_SUBCLASSES
from ..content_scoring import score_correspondence_content
from ..suites import _labelled_label_pairs
from .base import TypeScorer

#: Scalar keys this module adds to the payload (the nested topic/sentiment dicts stay out).
CONTENT_KEYS = (
    "content_topic_accuracy",
    "content_topic_f1_macro",
    "n_topic",
    "sentiment_accuracy",
    "sentiment_f1_macro",
    "n_sentiment",
)


class CorrespondenceScorer(TypeScorer):
    """Topic and sentiment scored exactly as the correspondence suite scores them."""

    def extras(
        self,
        predicted: Mapping[str, Any],
        raw_expected: Mapping[str, Any],
        *,
        subclass: str | None,
        doc_text: str | None,
    ) -> dict[str, Any]:
        """Topic / sentiment scores for each labelled expected label; else ``{}``."""
        kwargs: dict[str, Any] = {}
        for label, exp_kw, pred_kw in (
            ("content_topic", "expected_topic", "predicted_topic"),
            ("sentiment_label", "expected_sentiment", "predicted_sentiment"),
        ):
            # A blank expected label (``""``, ``None``, the literal ``"nan"``) is not
            # ground truth: the suite drops it, so this scorer does too.
            exp, pred = _labelled_label_pairs(raw_expected.get(label), predicted.get(label))
            if exp is not None:
                kwargs[exp_kw] = exp
                kwargs[pred_kw] = pred
        if not kwargs:
            return {}
        out = score_correspondence_content(**kwargs)
        return {k: out[k] for k in CONTENT_KEYS if k in out}


SCORER = CorrespondenceScorer(doc_type="correspondence", subclasses=DOC_TYPE_SUBCLASSES["correspondence"])
