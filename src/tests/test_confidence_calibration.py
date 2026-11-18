"""Calibrated confidence profile (2026-11-17) and its legacy A/B arm.

Pins the values fitted in docs/reports/evaluations/2026-11-17-confidence-calibration.md,
checks that ``MAILROOM_CONFIDENCE_PROFILE=legacy_v0`` restores the pre-calibration
routing exactly, and walks the routing decisions the change moves: the sorter ``high``
band, the merger judge band and the ``auto_archive: false`` park with its review cause.
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
        "contract": 0.95,
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
        ("contract", "extract", "retry_classify"),
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
