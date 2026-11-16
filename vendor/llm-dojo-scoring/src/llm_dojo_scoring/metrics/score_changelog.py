"""Machine-readable ledger of releases that moved a score (issue #97).

``scorer_version`` tells a comparison that two legs came from different
releases. This ledger tells it *what* changed, so a historical card can be
read without guessing whether a delta is a model effect or a scorer fix.

Every entry is a release, the canonical ``metric_id`` values it moved, and
whether stored runs must be re-scored before they can be compared. The table
is intentionally small: add an entry only when a deterministic scorer change
can move a number on unchanged predictions, or when a release adds metric ids
(a new-metrics entry says so, lists the ids and sets ``rescore_required`` to
``False``: no existing value moves). :func:`score_changes_between` returns both
kinds, so a reader that wants only the entries needing a re-score filters on
``rescore_required``.
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
        # Shipped in the v0.29.0 tag (f95c318); recorded after the release.
        "version": "0.29.0",
        "metric_ids": (
            "cuad.sandbox_category_detection.micro_f1",
            "cuad.sandbox_category_detection.micro_precision",
            "cuad.sandbox_category_detection.micro_recall",
            "pipeline.extraction.overall",
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
        ),
        "change": (
            "Date scoring no longer reads the clock (#134): only the components both "
            "sides state are compared, so a yearless or dayless GT scores the same in "
            "every run year; a prediction stating a year against a yearless GT scores at "
            "most 0.67; the 45-day window applies only to fully dated pairs; two-digit "
            "years use a fixed pivot (69-99 -> 1900s). Day ranges, a comma glued to the "
            "year and weekday names also parse differently, and a day that is a substring "
            "of the other is no longer a match ('11 August 2014' vs '1 August 2014': "
            "1.0 -> 0.67); other fully dated pairs that parse on both sides do not move. "
            "The sandbox CUAD check that an Effective Date equals the Agreement Date "
            "compares stated components, so a yearless agreement date no longer matches a "
            "dated prediction (cuad_fp can move). Diagnostics date R^2 and date_mae_days "
            "skip partial dates (no canonical metric_id covers them). In date-list matching "
            "and audit_scalar_field / audit_list_field, a dated prediction is no longer a "
            "match for a yearless label."
        ),
        "rescore_required": True,
    },
    {
        # Ships in 0.30.0: v0.29.0 was tagged from main (f95c318) before these changes landed.
        "version": "0.30.0",
        "metric_ids": (
            "pipeline.extraction.overall",
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
        ),
        "change": (
            "Money parsing reads a currency word next to a K/M/B suffix ('1.2M USD', "
            "'USD 1.2M'; #133). The same parser moves two metrics that have no canonical "
            "metric_id: amount_exactness (an unparsed pair was skipped, now scored) and the "
            "diagnostics money_mae_usd / money_median_ae_usd / money_mae_per_field / "
            "money_n_pairs (more pairs, so their values and counts change). On a CMS "
            "insurance row the subclass-derived cms_source_table is no longer scored when the prediction states no table "
            "(a perfect carrier extraction scored 0.9412; #132); a stated table, on "
            "cms_source_table or claim_type, is still scored."
        ),
        "rescore_required": True,
    },
    {
        # Second 0.30.0 change set (field-micro and label honesty on Hub rows).
        "version": "0.30.0",
        "metric_ids": (
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
            "maud.question.micro_accuracy",
            "pipeline.enron.topic_accuracy",
            "pipeline.enron.topic_f1_macro",
            "pipeline.enron.sentiment_accuracy",
            "pipeline.enron.sentiment_f1_macro",
        ),
        "change": (
            "Field-micro P/R/F1/F2 exclude a schema field the ground truth does not "
            "label on both sides (a correct value there was a false positive); an "
            "off-schema key, or a value on a field labelled empty (not_applicable, "
            "schema_documented_absence), is still one; a pending_annotation field is "
            "unlabelled and a value on it is not a false positive (except a CMS row's "
            "documented-absent adjuster / denial_reasons). A row "
            "with no labelled extraction field (presence-only CUAD, MAUD-only merger, "
            "no GT) gets them as null from the suite and doc scorers, not 0.0. A MAUD-only merger payload's "
            "overall score is null and its field-micro P/R/F1/F2 are null (they were 0.0); its "
            "headline metric_id is maud.question.micro_accuracy only while nothing is derivable. "
            "A blank or literal 'nan' content_topic / sentiment_label is not a label: "
            "no topic or sentiment score is emitted for it (it scored 0.0)."
        ),
        "rescore_required": True,
    },
    {
        # Third 0.30.0 change set (merger MAUD labels become extraction gold).
        "version": "0.30.0",
        "metric_ids": (
            "pipeline.extraction.overall",
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
            "maud.question.micro_accuracy",
        ),
        "change": (
            "A Hub merger row's maud_clause_labels are extraction gold: maud_clauses (one "
            "'<Question>: <Hub class>' line per answered question) and merger_consideration "
            "(the Type of Consideration answer, else the subclass) are scored by the suites, "
            "so such a row has an overall score and field-micro P/R/F1/F2 instead of null / 0.0 "
            "(its headline metric_id is pipeline.extraction.overall, as it was in 0.29.0). "
            "maud_clauses is scored question by question, not as fuzzy text. A question the gold skips as ambiguous (a multi-label or flagged "
            "answer, or case-variant keys with conflicting answers) is unscored on both sides: "
            "a prediction for it no longer counts as an unmatched prediction in precision. "
            "A question the Hub leaves blank or 'nan' is not ambiguous; answering it still "
            "does. maud.question.micro_accuracy itself keeps its value."
        ),
        "rescore_required": True,
    },
    {
        # Fourth 0.30.0 change set (new metrics only; no existing value moves).
        "version": "0.30.0",
        "metric_ids": (
            "decision.judge.verdict_accuracy",
            "decision.judge.macro_f1",
            "decision.judge.escalation_recall",
            "decision.judge.false_escalation_rate",
            "decision.judge.error_rate",
            "decision.arbiter.decision_accuracy",
            "decision.arbiter.retry_usefulness",
            "decision.arbiter.wrongful_accept_rate",
            "decision.arbiter.unnecessary_escalation_rate",
            "decision.boss.wrongful_approve_rate",
            "decision.boss.needless_review_rate",
            "decision.review_tray.wasted_review_rate",
            "decision.review_tray.correction_rate",
            "decision.review_tray.reject_rate",
        ),
        "change": (
            "New metrics: judge, arbiter, boss and review-tray decision lanes "
            "(scoring.decision_lanes). The review tray counts an approve_as_is that records "
            "edited or removed fields as correct in correction_rate, as every lane does. "
            "No existing metric value changes."
        ),
        "rescore_required": False,
    },
    {
        # Fifth 0.30.0 change set (new metrics only; no existing value moves).
        "version": "0.30.0",
        "metric_ids": (
            "ml.intake.doc_type_accuracy",
            "ml.intake.doc_type_macro_f1",
            "ml.intake.doc_type_ece",
            "ml.intake.subclass_accuracy",
            "ml.intake.subclass_macro_f1",
            "ml.intake.subclass_ece",
            "ml.intake.fast_path_coverage",
            "ml.intake.selective_risk",
            "ml.intake.selective_risk_upper",
        ),
        "change": (
            "New metrics: intake classifier accuracy, macro F1, calibration and "
            "fast-path risk (ml.score_intake_model). No existing metric value changes."
        ),
        "rescore_required": False,
    },
    {
        # Sixth 0.30.0 change set: the sandbox re-vendor follow-ups (#159-#162).
        "version": "0.30.0",
        "metric_ids": (
            "pipeline.extraction.overall",
            "pipeline.extraction.field_micro_f1",
            "pipeline.extraction.field_micro_f2",
            "pipeline.extraction.field_micro_precision",
            "pipeline.extraction.field_micro_recall",
            "maud.question.micro_accuracy",
            "ml.intake.selective_risk_upper",
            "decision.judge.verdict_accuracy",
            "decision.judge.macro_f1",
            "decision.judge.escalation_recall",
            "decision.judge.false_escalation_rate",
            "decision.arbiter.decision_accuracy",
            "decision.arbiter.retry_usefulness",
            "decision.arbiter.wrongful_accept_rate",
            "decision.arbiter.unnecessary_escalation_rate",
            "decision.boss.needless_review_rate",
            "decision.boss.wrongful_approve_rate",
            "decision.review_tray.correction_rate",
            "decision.review_tray.reject_rate",
            "decision.review_tray.wasted_review_rate",
        ),
        "change": (
            "A month-first or month-year date phrase ('August 18', 'August 2014') is found "
            "inside longer predicted text, ordinal suffixes aside (#162). An "
            "off-vocabulary MAUD 'Type of Consideration' answer (blank included) no longer "
            "ties at 'other' with a different answer, and two different ones in one "
            "prediction are a conflict (#160). The Wilson "
            "upper bound behind ml.intake.selective_risk_upper is rounded up, never to "
            "nearest (#160). Decision lanes: an attempt with extractions of several classes "
            "and no doc_type is unscored (ambiguous_attempt), a typed decision is graded by "
            "its own class's extraction, human gold grades only a decision on the class the "
            "person resolved (#160), and llm-mailroom "
            "RunRecord gold (human_decision approved/rejected) reads as a resolution (#159)."
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
