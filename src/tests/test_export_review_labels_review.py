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


def test_decision_lane_acceptance_is_an_overwrite_diagnostic():
    # T17: a decision-lane accept rewrites the parked manifest's class AND
    # classification_confidence with the REVIEWER's values (review_classify_node), so the
    # snapshot's numbers are the reviewer's. ``review_route`` is never persisted anywhere in
    # src, and the verdict itself is the tell for the class: attempt 1 is unrecoverable.
    label = _label(reviewer_doc_type="contract", reviewer_confidence=0.91,
                   classification_confidence=0.91)
    rec = _record(_manifest(label, review_verdict="decision_accepts"))
    assert rec.sorter == [] and rec.observed.sorter_attempts == 0
    assert rec.diagnostics["sorter_attempt_1"] == "sorter_attempt_1_overwritten_by_reviewer"


def test_decision_lane_acceptance_is_an_overwrite_without_review_route():
    # The vetted sketch's realistic park: the reviewer's re-labelled manifest as parked —
    # same class by the lane's own read, confidence rewritten to the reviewer's 0.91, and
    # no ``review_route`` anywhere (it is state-only, never written to the manifest).
    label = _label(reviewer_doc_type="contract", reviewer_confidence=0.91,
                   classification_confidence=0.91, snap_sub=None)
    manifest = _manifest(label, doc_type="contract", review_verdict="decision_accepts")
    rec = _record(manifest)
    assert rec.sorter == []
    assert rec.observed.sorter_attempts == 0
    assert rec.diagnostics["sorter_attempt_1"] == "sorter_attempt_1_overwritten_by_reviewer"
    assert rec.reviewer.ran and rec.reviewer.reviewer_doc_type == "contract"


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


# -- T1: the exported extraction attempt carries the schema/hollow evidence ----------------

# The replay test's candidate policy mirrors src/tests/test_policy_replay.py: classify high
# .95 / low .80, extraction auto-archive cut .90, retry_max 2. Taxonomy-independent.
POLICY = {
    "high": 0.95,
    "low": 0.80,
    "retry_max": 2,
    "judge_band_high": 0.90,
    "arbiter_retry_max": 2,
    "judge_max_passes": 3,
    "by_class": {},
}


def test_exported_extraction_records_schema_and_payload_evidence():
    # Hollow `{}`: live routing parked this as ``extraction_hollow_review``; the old export
    # replayed it as a confident, schema-clean attempt and it auto-archived instead.
    hollow = _record(_manifest(_label(extraction_confidence=0.99, extracted_data={},
                                     parse_error=False), doc_type="contract")).extraction[0]
    assert hollow.hollow is True and hollow.schema_valid is False

    # A payload that fails its class schema (`parties` is list[str] on ContractExtraction).
    invalid = _record(_manifest(_label(extraction_confidence=0.85, extracted_data={"parties": "A"}),
                                doc_type="contract")).extraction[0]
    assert invalid.hollow is False and invalid.schema_valid is False

    # A payload the contract schema accepts: the evidence says the attempt really passed.
    valid = _record(
        _manifest(_label(extraction_confidence=0.95,
                         extracted_data={"parties": ["A"], "agreement_date": "2026-01-01"}),
                  doc_type="contract")
    ).extraction[0]
    assert valid.hollow is False and valid.schema_valid is True

    # No payload dict in the snapshot: nothing to validate — schema_valid stays None
    # (routing skips the schema gate on a falsy extraction; absent is a pass).
    bare = _record(_manifest(_label(extraction_confidence=0.8), doc_type="contract")).extraction[0]
    assert bare.schema_valid is None and bare.hollow is False


def test_exported_parse_error_records_invalid_schema_evidence():
    # review_resolve.model_snapshot strips the ``_parse_error`` marker from extracted_data but
    # keeps its flag; the evidence must still say the extraction was parked invalid.
    att = _record(_manifest(_label(extraction_confidence=0.8, extracted_data={"parties": ["A"]},
                                   parse_error=True), doc_type="contract")).extraction[0]
    assert att.schema_valid is False and att.hollow is False


def _attempts_from(manifest):
    """The exported extraction attempts, plus a second (retry) attempt replay needs."""
    rec = _record(manifest)
    first = rec.extraction[0].model_copy(update={"attempt": 1, "doc_type": "contract"})
    retry = rec.extraction[0].model_copy(update={"attempt": 2, "confidence": 0.95,
                                                 "schema_valid": True, "hollow": False})
    return rec.model_copy(update={"extraction": [first, retry]})


def test_hollow_export_replays_as_a_retry_never_an_archive():
    # End-to-end against the same consumer (`policy_replay.replay`) T1 was written for:
    # the empty-payload park must engage the hollow gate's retry, not auto-archive attempt 1.
    from pipeline import policy_replay as pr

    from schemas.run_record import SorterAttempt

    hollow = _attempts_from(_manifest(_label(extraction_confidence=0.99, extracted_data={}),
                                      doc_type="contract"))
    sorter = SorterAttempt(attempt=1, doc_type="contract", confidence=0.97,
                           confidence_raw=0.97)
    out = pr.replay(hollow.model_copy(update={"sorter": [sorter]}), POLICY)
    assert "retry_extract" in out.path and out.outcome == pr.AUTO  # attempt 2 archives; attempt 1 never did


# -- #203 review: removed fields, parked attempt counts, decision route, rejections ----------


def test_a_legacy_complete_that_only_removes_a_field_is_a_correction():
    label = {**_label(resolution="correct"), "removed_fields": ["governing_law"]}
    assert exp.is_corrected(label) is True
    assert exp.is_corrected(_label()) is False
    summary = exp.summarize([_manifest(label), _manifest(_label())])
    assert summary["corrections"] == 1 and summary["correction_rate"] == 0.5


def test_the_parked_classification_attempt_count_beats_the_reset_manifest_value():
    # review_resolve snapshots the manifest as parked (classification_attempts=2); the approve
    # path then resets the live count to 0, so the manifest the exporter reads says "first attempt".
    parked_retry = _label(classification_attempts=2)
    rec = _record(_manifest(parked_retry, classification_attempts=0))
    assert rec.sorter == [] and rec.observed.sorter_attempts == 0
    assert rec.diagnostics["sorter_attempt_1"] == "sorter_attempt_1_replaced_by_retry"
    from pipeline import policy_replay as pr

    assert pr.sorter_pairs([rec]) == {}  # the retry's confidence never enters calibration
    # A snapshot that says attempt 1 is trusted over a later manifest count, and older snapshots
    # without the key keep reading the manifest.
    assert _record(_manifest(_label(classification_attempts=1), classification_attempts=2)).sorter
    assert _record(_manifest(_label(), classification_attempts=2)).sorter == []


def test_the_parked_extraction_attempt_number_is_exported_and_replay_cannot_use_it_as_attempt_one():
    from pipeline import policy_replay as pr
    from schemas.run_record import SorterAttempt

    label = _label(extraction_confidence=0.99, extraction_attempts=3,
                   extracted_data={"parties": ["A"], "agreement_date": "2026-01-01"})
    rec = _record(_manifest(label, doc_type="contract"))
    assert [a.attempt for a in rec.extraction] == [3]
    assert rec.extraction[0].confidence == 0.99
    assert rec.diagnostics["extraction_attempt_1"] == "extraction_attempt_1_replaced_by_retry"
    sorter = SorterAttempt(attempt=1, doc_type="contract", confidence=0.97, confidence_raw=0.97)
    out = pr.replay(rec.model_copy(update={"sorter": [sorter]}), POLICY)
    assert out.outcome == pr.UNOBSERVED and out.reasons == ["extraction_attempt_1_not_observed"]
    # older snapshots (no count) and a first attempt stay attempt 1
    legacy = _record(_manifest(_label(extraction_confidence=0.9), doc_type="contract"))
    assert [a.attempt for a in legacy.extraction] == [1] and "extraction_attempt_1" not in legacy.diagnostics
    first = _record(_manifest(_label(extraction_confidence=0.9, extraction_attempts=1), doc_type="contract"))
    assert [a.attempt for a in first.extraction] == [1]


def test_a_parse_failure_without_a_payload_is_still_schema_invalid():
    # model_snapshot stores extracted_data=None when only internal keys were left after the parse error
    rec = _record(_manifest(_label(extraction_confidence=0.8, extracted_data=None, parse_error=True),
                            doc_type="contract"))
    assert rec.extraction[0].schema_valid is False
    # no payload and no parse error is still "nothing to validate"
    bare = _record(_manifest(_label(extraction_confidence=0.8, extracted_data=None, parse_error=False),
                             doc_type="contract"))
    assert bare.extraction[0].schema_valid is None


def test_a_reclassified_snapshot_is_validated_against_the_class_it_was_extracted_as():
    # The parked extraction ran as ``contract`` (``parties`` must be a list); the reviewer then
    # re-labelled the manifest, so manifest.doc_type is the new class.
    label = _label(snap_class="contract", human_class="correspondence", resolution="reclassify",
                   extraction_confidence=0.9, extracted_data={"parties": "A"})
    att = _record(_manifest(label, doc_type="correspondence")).extraction[0]
    assert att.schema_valid is False
    assert att.doc_type == "contract"  # replay for the final class then finds no attempt to reuse


def test_extraction_without_a_snapshot_class_falls_back_to_the_manifest_class():
    label = _label(extraction_confidence=0.9, extracted_data={"parties": "A"})
    label["model_snapshot"]["doc_type"] = None
    att = _record(_manifest(label, doc_type="contract")).extraction[0]
    assert att.schema_valid is False and att.doc_type == "contract"


def test_decision_lane_verdict_and_route_reach_the_exported_reviewer(monkeypatch):
    import pipeline.config as pconfig
    from pipeline import policy_replay as pr

    label = _label(reviewer_doc_type="correspondence", reviewer_confidence=0.55)
    rec = _record(_manifest(label, review_verdict="decision_escalates"))
    assert rec.reviewer.mode == "decision_only"
    assert rec.reviewer.reviewer_verdict == "decision_escalates"
    assert rec.reviewer.review_route == "human_review"
    accepted = _record(_manifest(_label(reviewer_doc_type="contract", reviewer_confidence=0.91,
                                        classification_confidence=0.91), review_verdict="decision_accepts"))
    assert accepted.reviewer.mode == "decision_only" and accepted.reviewer.review_route == "extract"
    # replay follows the observed decision instead of reporting decision_route_not_observed
    monkeypatch.setattr(pconfig, "get_review_mode", lambda _cls=None: "decision_only")
    assert pr.reviewer_route(rec) == "human_review"


def test_llm_lane_reviewer_fields_are_unchanged():
    label = _label(reviewer_doc_type="contract", reviewer_confidence=0.6)
    rec = _record(_manifest(label, review_verdict="reviewer_agrees_low"))
    assert rec.reviewer.mode is None and rec.reviewer.reviewer_verdict is None
    assert rec.reviewer.review_route is None
    assert _record(_manifest(label)).reviewer.review_route is None


def test_an_unconfirmed_rejection_is_not_sorter_gold():
    from pipeline import policy_replay as pr

    guess = {**_label(), "decision": "rejected", "resolution": "reject"}
    rec = _record(_manifest(guess, classification_attempts=1))
    assert rec.gold.expected_doc_type is None and rec.gold.human_decision == "rejected"
    assert pr.sorter_pairs([rec]) == {}  # the model's own guess is not a correct label
    # a person who named the class (override, or a class the model did not propose) confirmed it
    overridden = {**guess, "class_override": {"doc_type": "contract"}}
    assert _record(_manifest(overridden, classification_attempts=1)).gold.expected_doc_type == "contract"
    changed = {**_label(human_class="correspondence"), "decision": "rejected", "resolution": "reject"}
    assert _record(_manifest(changed, classification_attempts=1)).gold.expected_doc_type == "correspondence"
    # approvals are untouched
    approved = _record(_manifest(_label(), classification_attempts=1))
    assert approved.gold.expected_doc_type == "contract" and pr.sorter_pairs([approved])
