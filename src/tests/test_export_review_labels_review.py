"""Review-driven hardening of scripts/export_review_labels.py (PR #203 threads T30-T33)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_review_labels as exp  # noqa: E402


def _label(*, human_class="contract", human_sub=None, snap_class="contract", snap_sub=None,
           resolution="approve_as_is", corrections=None, **snap_extra):
    snap = {"doc_type": snap_class, "doc_subclass": snap_sub, "classification_confidence": 0.8,
            "extraction_confidence": 0.7, **snap_extra}
    return {"decision": "approved", "resolution": resolution, "doc_type": human_class,
            "doc_subclass": human_sub, "field_corrections": corrections, "model_snapshot": snap,
            "resolved_at": "2026-06-01T10:00:00+00:00"}


def _manifest(label, **extra):
    return {"doc_id": "d1", "original_filename": "d1.pdf", "stage": "archived", "human_label": label, **extra}


# -- T30 / T31: a subclass-only correction is a correction, not a wasted review -----------


def test_subclass_only_change_counts_as_a_correction_and_not_a_wasted_review():
    label = _label(human_sub="nda", snap_sub="license", resolution="reclassify")
    assert exp.is_corrected(label) is True
    assert exp.is_wasted({**label, "resolution": "approve_as_is"}) is False


def test_setting_and_clearing_a_subclass_are_corrections_too():
    assert exp.is_corrected(_label(human_sub="nda", snap_sub=None))
    assert exp.is_corrected(_label(human_sub=None, snap_sub="license"))


def test_unchanged_subclass_is_still_a_wasted_review():
    label = _label(human_sub="license", snap_sub="license")
    assert exp.is_corrected(label) is False and exp.is_wasted(label) is True
    assert exp.is_wasted(_label()) is True  # None == None


def test_blank_subclass_equals_none():
    assert exp.is_corrected(_label(human_sub="", snap_sub=None)) is False


def test_snapshot_without_a_subclass_key_says_nothing_about_it():
    label = _label(human_sub="nda")
    del label["model_snapshot"]["doc_subclass"]
    assert exp.is_corrected(label) is False and exp.is_wasted(label) is True


def test_class_and_field_corrections_keep_working():
    assert exp.is_corrected(_label(human_class="correspondence"))
    assert exp.is_corrected(_label(corrections={"parties": ["A"]}))
    assert not exp.is_wasted(_label(corrections={"parties": ["A"]}))


def test_summary_counts_subclass_only_edit_as_correction():
    m1 = _manifest(_label(human_sub="nda", snap_sub="license", resolution="reclassify"))
    m2 = _manifest(_label())
    s = exp.summarize([m1, m2])
    assert s["corrections"] == 1 and s["wasted_reviews"] == 1


# -- T32: attempt 1 is never a later stage's value -----------------------------------------


def _record(manifest):
    return exp.to_run_record(manifest, "run")


def test_plain_sorter_snapshot_is_attempt_one():
    rec = _record(_manifest(_label(snap_sub="license"), classification_attempts=1))
    assert rec.sorter[0].doc_type == "contract" and rec.sorter[0].confidence == 0.8
    assert rec.sorter[0].doc_subclass == "license" and rec.observed.sorter_attempts == 1
    assert rec.diagnostics == {}


def test_reviewer_that_disagrees_low_leaves_attempt_one_intact():
    rec = _record(_manifest(_label(reviewer_doc_type="correspondence", reviewer_confidence=0.6),
                            review_verdict="reviewer_conflicts"))
    assert rec.sorter[0].doc_type == "contract" and rec.sorter[0].confidence == 0.8


def test_winning_reviewer_override_erases_attempt_one():
    label = _label(snap_class="correspondence", reviewer_doc_type="correspondence", reviewer_confidence=0.99,
                   classification_confidence=0.99)
    rec = _record(_manifest(label, review_verdict="reviewer_overrides"))
    assert rec.sorter == [] and rec.observed.sorter_attempts == 0
    assert rec.diagnostics["sorter_attempt_1"] == "sorter_attempt_1_overwritten_by_reviewer"
    assert rec.reviewer.ran and rec.reviewer.reviewer_confidence == 0.99  # the reviewer lane is untouched


def test_reviewer_agrees_high_keeps_the_class_but_not_the_overwritten_numbers():
    label = _label(reviewer_doc_type="contract", reviewer_confidence=0.99, classification_confidence=0.99,
                   doc_subclass="other")
    rec = _record(_manifest(label, review_verdict="reviewer_agrees_high"))
    first = rec.sorter[0]
    assert first.doc_type == "contract" and first.confidence is None and first.doc_subclass is None
    assert rec.observed.sorter_attempts == 1
    assert rec.diagnostics["sorter_attempt_1"] == "sorter_confidence_overwritten_by_reviewer"


def test_old_manifest_without_a_verdict_is_caught_by_the_matching_pair():
    label = _label(reviewer_doc_type="contract", reviewer_confidence=0.8)  # snapshot shows the reviewer's pair
    rec = _record(_manifest(label))
    assert rec.sorter == []


def test_decision_lane_extract_route_overwrote_the_sorter_values():
    label = _label(reviewer_doc_type="contract", reviewer_confidence=0.91)
    rec = _record(_manifest(label, review_verdict="decision_accept", review_route="extract"))
    assert rec.sorter == []


def test_retry_replaced_attempt_one():
    rec = _record(_manifest(_label(), classification_attempts=2))
    assert rec.sorter == [] and rec.observed.sorter_attempts == 0
    assert rec.diagnostics["sorter_attempt_1"] == "sorter_attempt_1_replaced_by_retry"


def test_unobserved_sorter_is_skipped_by_calibration_pairs():
    from pipeline import policy_replay as pr

    rec = _record(_manifest(_label(), classification_attempts=2))
    assert pr.sorter_pairs([rec]) == {}


# -- T33: the extraction-attempt guard is deliberate ---------------------------------------


def test_human_sourced_extraction_is_not_exported_and_model_sourced_is():
    label = _label(extraction_confidence=0.7)
    assert _record(_manifest(label, extraction_source="human")).extraction == []
    assert len(_record(_manifest(label, extraction_source=None)).extraction) == 1
