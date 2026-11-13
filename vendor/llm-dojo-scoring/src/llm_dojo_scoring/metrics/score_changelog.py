"""Machine-readable ledger of releases that moved a score (issue #97).

``scorer_version`` tells a comparison that two legs came from different
releases. This ledger tells it *what* changed, so a historical card can be
read without guessing whether a delta is a model effect or a scorer fix.

Every entry is a release, the canonical ``metric_id`` values it moved, and
whether stored runs must be re-scored before they can be compared. The table
is intentionally small: add an entry only when a deterministic scorer change
can move a number on unchanged predictions.
"""

from __future__ import annotations

from typing import Any

from packaging.version import InvalidVersion, Version

__all__ = ["SCORE_CHANGELOG", "score_changes_between", "score_changes_for_metric"]

#: Release → metrics whose values can move on unchanged predictions.
SCORE_CHANGELOG: tuple[dict[str, Any], ...] = (
    {
        "version": "0.22.0",
        "metric_ids": (
            "maud.question.micro_accuracy",
            "maud.clause_presence.rate",
            "pipeline.extraction.overall",
        ),
        "change": "MAUD fold/reducer and six MAUD truth defects fixed.",
        "rescore_required": True,
    },
    {
        "version": "0.25.0",
        # Subclass scores only: doc-type accuracy / f1_macro are computed
        # before subclass scoring and did not move. No canonical pipeline
        # subclass metric_id exists yet, so this entry names none.
        "metric_ids": (),
        "change": (
            "Merger subclass 'other' is unannotated and excluded from subclass "
            "scoring. Affects subclass scores only; no canonical metric_id covers them."
        ),
        "rescore_required": True,
    },
    {
        "version": "0.26.0",
        "metric_ids": (
            "cuad.clause_presence.micro_f1",
            "cuad.sandbox_category_detection.micro_f1",
            "pipeline.extraction.overall",
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
        ),
        "change": (
            "CUAD parties strip defined-term annotations and effective_date may use "
            "the agreement date; insurance scores cms_source_table orthogonally. "
            "#93: on CMS rows claim_type is an exact product-line label (a table "
            "token no longer folds onto health), cms_source_table is derived from "
            "expected_subclass, adjuster/denial_reasons are documented-absent "
            "(a stated value scores 0) and supporting_documents match by identifier."
        ),
        "rescore_required": True,
    },
    {
        # Proposed next release; the version is cut separately with release.py.
        "version": "0.29.0",
        "metric_ids": (
            "pipeline.extraction.overall",
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
        ),
        "change": (
            "Money parsing reads a currency word next to a K/M/B suffix ('1.2M USD', "
            "'USD 1.2M'; #133). On a CMS insurance row the subclass-derived "
            "cms_source_table is no longer scored when the prediction states no table "
            "(a perfect carrier extraction scored 0.9412; #132); a stated table, on "
            "cms_source_table or claim_type, is still scored."
        ),
        "rescore_required": True,
    },
)


def _version(value: str | None) -> Version | None:
    """Parse a release string, returning ``None`` for absent or invalid values."""
    if value is None:
        return None
    try:
        return Version(str(value).strip())
    except InvalidVersion:
        return None


def score_changes_between(
    before: str | None,
    after: str | None,
    *,
    metric_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return ledger entries in ``(before, after]``, optionally for one metric.

    A missing or unparseable release returns an empty list: the comparison
    already reports ``scorer_version_unverified`` separately. Reversing the
    arguments returns the same entries, sorted oldest first.
    """
    left, right = _version(before), _version(after)
    if left is None or right is None or left == right:
        return []
    low, high = (left, right) if left < right else (right, left)
    entries: list[dict[str, Any]] = []
    for entry in SCORE_CHANGELOG:
        version = _version(entry.get("version"))
        if version is None or not (low < version <= high):
            continue
        if metric_id is not None and metric_id not in entry["metric_ids"]:
            continue
        entries.append(dict(entry))
    return entries


def score_changes_for_metric(
    metric_id: str, before: str | None, after: str | None
) -> list[dict[str, Any]]:
    """Return ledger entries that moved one metric between two releases."""
    return score_changes_between(before, after, metric_id=metric_id)
