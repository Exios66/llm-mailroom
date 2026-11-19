"""Calibrated confidence profile (2026-11-17) and its legacy A/B arm.

Pins the values fitted in docs/reports/evaluations/2026-11-17-confidence-calibration.md,
checks that ``MAILROOM_CONFIDENCE_PROFILE=legacy_v0`` restores the pre-calibration
thresholds, and walks the routing decisions the change moves: the sorter ``high``
band, the merger judge band and the ``auto_archive: false`` park with its review cause.
The archive-check keys stay ``false`` on both profiles (they are not part of the
calibration delta).
"""

from __future__ import annotations

import pytest

from graph.routing import (
    after_classify,
    after_report,
    after_review_classify,
    judge_gate,
)
from pipeline.config import (
    CONFIDENCE_PROFILE_ENV,
    class_auto_archives,
    confidence_profile_name,
    get_confidence_thresholds,
    load_config,
)
from pipeline.reconsideration import CLASS_REQUIRES_REVIEW, collect_review_causes

CLASSES = ("contract", "merger_agreement", "insurance_claim", "corporate_record", "correspondence")

# The `confidence` block as it stood before the calibration, verbatim.
LEGACY_V0 = {
    "high": 0.97,
    "low": 0.88,
    "retry_max": 2,
    "judge_band_high": 0.95,
    "arbiter_retry_max": 2,
    "judge_max_passes": 3,
    "judge_rejudge_retry": False,
    "keep_best_attempt": False,
    "judge_complete_archives": False,
    "arbiter_accept_archives": False,
    "judge_clears_low_confidence": False,
    "conflict_threshold": 0.3,
    "by_class": {
        "contract": {"high": 0.98, "low": 0.90, "judge_band_high": 0.97},
        "merger_agreement": {"high": 0.98, "low": 0.90, "judge_band_high": 0.97},
        "insurance_claim": {"high": 0.98, "low": 0.90, "judge_band_high": 0.97},
        "corporate_record": {"high": 0.96, "low": 0.86, "judge_band_high": 0.94},
        "correspondence": {"high": 0.95, "low": 0.85, "judge_band_high": 0.92},
    },
}


@pytest.fixture
def legacy(monkeypatch):
    monkeypatch.setenv(CONFIDENCE_PROFILE_ENV, "legacy_v0")


@pytest.fixture(autouse=True)
def _default_profile(monkeypatch):
    monkeypatch.delenv(CONFIDENCE_PROFILE_ENV, raising=False)


def _report_state(doc_type: str, **extra) -> dict:
    return {
        "doc_id": "doc-1",
        "doc_type": doc_type,
        "extracted_data": {"title": "x", "_report": {"summary": "ok"}},
        "extraction_confidence": 0.95,
        **extra,
    }


# --- profile selection -------------------------------------------------------------


def test_default_profile_is_calibrated():
    assert confidence_profile_name() == "calibrated_v1"


@pytest.mark.parametrize("raw", ["", "  ", "calibrated_v1"])
def test_blank_or_default_name_selects_the_confidence_block(monkeypatch, raw):
    monkeypatch.setenv(CONFIDENCE_PROFILE_ENV, raw)
    assert confidence_profile_name() == "calibrated_v1"
    assert get_confidence_thresholds("insurance_claim")["high"] == 0.95


def test_unknown_profile_is_an_error(monkeypatch):
    monkeypatch.setenv(CONFIDENCE_PROFILE_ENV, "legacy-v0")  # typo: dash, not underscore
    with pytest.raises(ValueError, match="not a confidence profile"):
        get_confidence_thresholds("contract")


def test_legacy_profile_is_the_pre_calibration_block_verbatim():
    assert load_config()["confidence_profiles"]["legacy_v0"] == LEGACY_V0


def test_legacy_profile_carries_every_global_key_of_the_active_block():
    active = {k for k in load_config()["confidence"] if k != "by_class"}
    legacy = {k for k in LEGACY_V0 if k != "by_class"}
    assert active == legacy


@pytest.mark.parametrize("doc_type", [None, *CLASSES])
def test_legacy_profile_restores_old_thresholds(legacy, doc_type):
    expected = {k: v for k, v in LEGACY_V0.items() if k != "by_class"}
    if doc_type:
        expected.update(LEGACY_V0["by_class"][doc_type])
    got = get_confidence_thresholds(doc_type)
    assert {k: got.get(k) for k in expected} == expected
    assert class_auto_archives(doc_type) is True


# --- calibrated values -------------------------------------------------------------


def test_calibrated_values():
    t = {c: get_confidence_thresholds(c) for c in CLASSES}
    assert {c: t[c]["high"] for c in CLASSES} == {
        "contract": 0.98,  # held back: 0.95 triples held-out wrong-class archives
        "merger_agreement": 0.98,
        "insurance_claim": 0.95,
        "corporate_record": 0.95,
        "correspondence": 0.95,
    }
    # `low` and the judge bands are unchanged except the merger band, which is empty.
    assert {c: t[c]["low"] for c in CLASSES} == {
        c: LEGACY_V0["by_class"][c]["low"] for c in CLASSES
    }
    for c in ("contract", "insurance_claim", "corporate_record", "correspondence"):
        assert t[c]["judge_band_high"] == LEGACY_V0["by_class"][c]["judge_band_high"]
    merger = t["merger_agreement"]
    assert merger["judge_band_high"] <= merger["low"]
    assert (merger["arbiter_retry_max"], merger["judge_max_passes"]) == (1, 2)
    assert t["contract"]["arbiter_retry_max"] == 2  # merger budgets do not leak
    assert [c for c in CLASSES if not class_auto_archives(c)] == ["merger_agreement"]
    assert class_auto_archives(None) is True
    assert class_auto_archives("unknown") is True


# --- routing: sorter band ----------------------------------------------------------


@pytest.mark.parametrize(
    "doc_type, calibrated, old",
    [
        ("contract", "retry_classify", "retry_classify"),  # held at 0.98: the sorter's 0.95 errs on contracts
        ("insurance_claim", "extract", "retry_classify"),
        ("corporate_record", "extract", "retry_classify"),
        ("correspondence", "extract", "extract"),
        ("merger_agreement", "retry_classify", "retry_classify"),
    ],
)
def test_sorter_at_its_usual_095(monkeypatch, doc_type, calibrated, old):
    """The sorter answers 0.95 on most documents; only the band around it moved."""
    state = {"classification_confidence": 0.95, "classification_attempts": 1, "doc_type": doc_type}
    assert after_classify(state) == calibrated
    monkeypatch.setenv(CONFIDENCE_PROFILE_ENV, "legacy_v0")
    assert after_classify(state) == old


@pytest.mark.parametrize("doc_type", CLASSES)
def test_sorter_below_low_still_retries_then_reviews(doc_type):
    state = {"classification_confidence": 0.5, "classification_attempts": 1, "doc_type": doc_type}
    assert after_classify(state) == "retry_classify"
    assert after_classify({**state, "classification_attempts": 3}) == "human_review"


def test_reviewer_agreeing_at_095_continues_for_insurance_only_when_calibrated(monkeypatch):
    state = {
        "doc_id": "doc-1",
        "doc_type": "insurance_claim",
        "review_verdict": "reviewer_agrees_high",
        "reviewer_confidence": 0.95,
        "reviewer_doc_type": "insurance_claim",
    }
    assert after_review_classify(state) == "extract"
    monkeypatch.setenv(CONFIDENCE_PROFILE_ENV, "legacy_v0")
    assert after_review_classify(state) == "human_review"


# --- routing: judge band and archive -----------------------------------------------


def test_merger_judge_band_is_empty_when_calibrated(legacy, monkeypatch):
    state = {"doc_type": "merger_agreement", "extraction_confidence": 0.95}
    assert judge_gate(state) is True  # legacy: 0.90 <= 0.95 < 0.97
    monkeypatch.delenv(CONFIDENCE_PROFILE_ENV)
    for conf in (0.0, 0.89, 0.9, 0.95, 0.99, 1.0):
        assert judge_gate({**state, "extraction_confidence": conf}) is False


@pytest.mark.parametrize("conf", [0.9, 0.95])
def test_contract_judge_band_unchanged(conf):
    assert judge_gate({"doc_type": "contract", "extraction_confidence": conf}) is True
    assert judge_gate({"doc_type": "contract", "extraction_confidence": 0.98}) is False


@pytest.mark.parametrize("doc_type", ["contract", "insurance_claim", "corporate_record", "correspondence"])
def test_other_classes_still_archive(doc_type):
    assert after_report(_report_state(doc_type)) == "catalog_write"


def test_merger_report_goes_to_a_person():
    assert after_report(_report_state("merger_agreement")) == "human_review"


def test_merger_archives_under_legacy(legacy):
    assert after_report(_report_state("merger_agreement")) == "catalog_write"


def test_merger_fields_a_person_applied_archive():
    state = _report_state(
        "merger_agreement", review_decision="human_applied", extraction_source="human"
    )
    assert after_report(state) == "catalog_write"


def test_merger_reextraction_after_class_approval_returns_to_a_person():
    # "approved" confirms the class and re-extracts: fresh model output, so it parks again.
    state = _report_state("merger_agreement", review_decision="approved")
    assert after_report(state) == "human_review"


def test_failed_report_still_parks_first():
    state = _report_state("contract")
    state["extracted_data"] = {"title": "x"}
    assert after_report(state) == "human_review"


# --- review cause ------------------------------------------------------------------


def test_merger_park_names_its_cause():
    causes = collect_review_causes(_report_state("merger_agreement"))
    assert CLASS_REQUIRES_REVIEW in causes


@pytest.mark.parametrize("doc_type", ["contract", "insurance_claim"])
def test_no_policy_cause_for_archiving_classes(doc_type):
    assert CLASS_REQUIRES_REVIEW not in collect_review_causes(_report_state(doc_type))


def test_no_policy_cause_once_a_person_applied_fields():
    state = _report_state(
        "merger_agreement", review_decision="human_applied", extraction_source="human"
    )
    assert CLASS_REQUIRES_REVIEW not in collect_review_causes(state)


def test_no_policy_cause_under_legacy(legacy):
    assert CLASS_REQUIRES_REVIEW not in collect_review_causes(_report_state("merger_agreement"))


def test_no_policy_cause_before_extraction():
    state = {"doc_id": "doc-1", "doc_type": "merger_agreement", "classification_confidence": 0.5}
    assert CLASS_REQUIRES_REVIEW not in collect_review_causes(state)


def test_policy_cause_has_a_queue_weight():
    weights = load_config()["review_queue"]["cause_weights"]
    assert CLASS_REQUIRES_REVIEW in weights


# --- end to end: a parked merger that a person approves archives -------------------


@pytest.mark.parametrize("resolution", ["approve_as_is", "correct"])
def test_parked_merger_archives_once_a_person_applies_fields(temp_base_dir, monkeypatch, resolution):
    """The policy park must not loop: the person's fields go through compile_report and archive."""
    from graph import build_graph as bg
    from pipeline.bins import review_dir, save_manifest
    from schemas.manifest import DocumentManifest, PipelineStage

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MAILROOM_MEMORY_DIR", str(temp_base_dir / "memory"))
    fields = {
        "parties": ["Acquirer Corp", "Target Inc"],
        "acquirer": "Acquirer Corp",
        "target": "Target Inc",
        "merger_consideration": "cash",
    }
    manifest = DocumentManifest(
        doc_id="merger-park-1",
        matter_id="CAL-1",
        original_filename="merger.txt",
        stage=PipelineStage.REVIEW,
        doc_type="merger_agreement",
        classification_confidence=0.99,
        extracted_data=dict(fields),
        extraction_confidence=1.0,
        escalation_reason="reconsider: this class never archives without a person",
        review_causes=[CLASS_REQUIRES_REVIEW],
        extraction_attempts=1,
    )
    review_file = review_dir() / "merger.txt"
    review_file.write_text("Agreement and Plan of Merger between Acquirer Corp and Target Inc.")
    save_manifest(manifest)
    bg.reset_compiled_graph()

    applied = dict(fields, merger_consideration="mixed") if resolution == "correct" else dict(fields)
    out = bg.resume_from_review(
        manifest,
        review_file,
        resolution={"decision": "approved", "resolution": resolution, "extracted_data": applied},
    )
    assert out["stage"] == "archived"
    assert out["extraction_source"] == "human"


# --- profile-aware readers ---------------------------------------------------------


def test_replay_override_wins_over_a_named_profile(legacy):
    from pipeline.policy_replay import current_policy, policy_override

    assert current_policy()["by_class"]["contract"]["high"] == 0.98  # legacy in force
    with policy_override({"by_class": {"contract": {"high": 0.5}}}):
        assert get_confidence_thresholds("contract")["high"] == 0.5
    assert confidence_profile_name() == "legacy_v0"  # restored afterwards
    assert get_confidence_thresholds("contract")["high"] == 0.98


def test_policy_expected_stage():
    from pipeline.config import policy_expected_stage

    assert policy_expected_stage("merger_agreement", "archived") == "review"
    assert policy_expected_stage("merger_agreement", "failed") == "failed"
    assert policy_expected_stage("contract", "archived") == "archived"
    assert policy_expected_stage(None, None) is None


def test_policy_expected_stage_under_legacy(legacy):
    from pipeline.config import policy_expected_stage

    assert policy_expected_stage("merger_agreement", "archived") == "archived"


def test_policy_cause_does_not_hide_a_real_failure():
    state = _report_state("merger_agreement", arbiter_decision="escalate")
    causes = collect_review_causes(state)
    assert "arbiter_escalated" in causes
    assert CLASS_REQUIRES_REVIEW not in causes


# --- review findings: merger judge loop, triage lane, scorers, causes ---------------


def test_merger_never_reaches_the_judge_loop():
    from graph.routing import after_extraction, judge_should_run

    state = {
        "doc_id": "d",
        "doc_type": "merger_agreement",
        "extracted_data": {"parties": ["A", "B"], "acquirer": "A"},
        "extraction_confidence": 0.5,
        "extraction_attempts": 3,
    }
    assert judge_should_run(state) is False
    assert after_extraction(state) == "human_review"


def test_other_classes_still_judge_an_unsure_extraction():
    from graph.routing import after_extraction

    state = {
        "doc_id": "d",
        "doc_type": "contract",
        "extracted_data": {"document_name": "x", "parties": ["A", "B"]},
        "extraction_confidence": 0.5,
        "extraction_attempts": 3,
    }
    assert after_extraction(state) == "judge_verify"


def test_policy_cause_survives_ground_truth_only_causes():
    state = _report_state("merger_agreement")
    state["ground_truth"] = {"expected_doc_class": "contract"}  # would add class_miss
    causes = collect_review_causes(state)
    assert "class_miss" in causes and CLASS_REQUIRES_REVIEW in causes


def test_stage_correct_expects_review_for_a_policy_class():
    from observability.scores import compute_pipeline_scores

    def stage_score(stage):
        state = {
            "doc_type": "merger_agreement",
            "stage": stage,
            "ground_truth": {"expected_doc_class": "merger_agreement", "expected_stage": "archived"},
        }
        return compute_pipeline_scores(state).get("stage_correct")

    assert stage_score("review") == 1
    assert stage_score("archived") == 0


def test_experiment_stage_evaluator_expects_review_for_a_policy_class():
    from scripts.run_experiment import stage_correct_evaluator

    expected = {"expected_doc_class": "merger_agreement", "expected_stage": "archived"}
    assert stage_correct_evaluator(input=None, output={"stage": "review"}, expected_output=expected).value == 1
    expected = {"expected_doc_class": "contract", "expected_stage": "archived"}
    assert stage_correct_evaluator(input=None, output={"stage": "review"}, expected_output=expected).value == 0


def test_unknown_profile_stops_the_watcher_before_it_starts(monkeypatch, temp_base_dir):
    from pipeline.watcher import Watcher

    monkeypatch.setenv(CONFIDENCE_PROFILE_ENV, "no_such_profile")
    with pytest.raises(ValueError, match="not a confidence profile"):
        Watcher().start()


# ── review resolution of a parked policy-class document ───────────────────


def _parked_merger(extracted):
    from schemas.manifest import DocumentManifest, PipelineStage

    before = DocumentManifest(
        doc_id="merger-plan-1", matter_id="M", original_filename="m.txt",
        stage=PipelineStage.REVIEW, doc_type="merger_agreement", extracted_data=extracted,
    )
    return before, before.model_copy(deep=True)


def test_legacy_plain_approval_of_a_parked_merger_is_refused_not_looped():
    """approved+resume re-extracts, `after_report` parks the merger again, and every click
    would cost a full extraction. The request is refused and names the archiving ways."""
    from pipeline.review_resolve import plan_resolution

    before, manifest = _parked_merger({"parties": ["Acme"], "confidence": 0.9})
    with pytest.raises(ValueError, match="never archived by a plain approval"):
        plan_resolution(before=before, manifest=manifest, class_override={}, decision="approved")


def test_structured_approval_of_a_parked_merger_archives_the_parked_fields():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _parked_merger({"parties": ["Acme"], "confidence": 0.9})
    plan = plan_resolution(
        before=before, manifest=manifest, class_override={},
        decision="approved", resolution="approve_as_is",
    )
    assert plan.route == "archive" and plan.extracted_data


def test_legacy_approval_without_parked_fields_still_confirms_the_class():
    from pipeline.review_resolve import plan_resolution

    before, manifest = _parked_merger(None)
    plan = plan_resolution(before=before, manifest=manifest, class_override={}, decision="approved")
    assert plan.route == "reextract"


def test_legacy_plain_approval_of_an_auto_archive_class_still_re_extracts():
    from pipeline.review_resolve import plan_resolution
    from schemas.manifest import DocumentManifest, PipelineStage

    before = DocumentManifest(
        doc_id="c-1", matter_id="M", original_filename="c.txt", stage=PipelineStage.REVIEW,
        doc_type="contract", extracted_data={"parties": ["Acme"], "confidence": 0.9},
    )
    plan = plan_resolution(
        before=before, manifest=before.model_copy(deep=True), class_override={}, decision="approved",
    )
    assert plan.route == "reextract"


def test_smoke_extract_check_follows_the_ground_truth_stage_for_a_policy_class():
    """A merger is expected to park, but a park before extraction is still a failure."""
    from scripts.smoke_live_documents import _checks

    row = {"expected_doc_class": "merger_agreement", "expected_stage": "archived"}
    result = {"doc_type": "merger_agreement", "stage": "review"}

    def journal(sequence):
        return {"node_sequence": sequence, "events": [1], "persisted": True, "outcome": {"stage": "review"}}

    ran = _checks(row, result, journal(["intake", "classify", "extract", "compile_report", "human_review"]))
    parked_early = _checks(row, result, journal(["intake", "classify", "human_review"]))
    assert ran["stage_ok"] and ran["extract_present"]
    assert parked_early["stage_ok"] and not parked_early["extract_present"]
