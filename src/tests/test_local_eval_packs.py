"""Local eval packs: insurance contrast, compliance Hub-zero, corporate extraction."""

from observability.honest_gaps import (
    determination_consistency_is_quality,
    insurance_expected_set_is_homogeneous,
    insurance_gt_is_homogeneous,
)
from observability.local_eval_packs import (
    all_local_pack_samples,
    compliance_local_samples,
    corporate_extraction_samples,
    insurance_contrast_samples,
    local_pack_status,
    score_local_packs,
)


def test_insurance_contrast_pack_is_mixed_and_non_homogeneous():
    samples = insurance_contrast_samples()
    assert len(samples) == 3
    dets = {
        (s["expected_fields"]["coverage_determination"])
        for s in samples
    }
    assert dets == {"approved", "denied", "partial"}
    denied = next(s for s in samples if s["expected_fields"]["coverage_determination"] == "denied")
    assert denied["expected_fields"]["denial_reasons"]
    assert insurance_expected_set_is_homogeneous(
        [s["expected_fields"] for s in samples]
    ) is False
    assert insurance_gt_is_homogeneous(denied["expected_fields"]) is False
    approved = next(s for s in samples if s["expected_fields"]["coverage_determination"] == "approved")
    assert insurance_gt_is_homogeneous(approved["expected_fields"]) is True
    assert determination_consistency_is_quality(approved["expected_fields"]) is False
    assert determination_consistency_is_quality(denied["expected_fields"]) is True


def test_cms_shaped_gt_is_homogeneous():
    cms = [
        {"coverage_determination": "approved", "denial_reasons": []},
        {"coverage_determination": "approved", "denial_reasons": []},
    ]
    assert insurance_expected_set_is_homogeneous(cms) is True
    assert determination_consistency_is_quality(cms[0]) is False


def test_score_local_packs_exercises_scorer_not_hub_accuracy():
    packs = score_local_packs()
    contrast = packs["insurance_contrast"]
    assert contrast["source"] == "local"
    assert contrast["mock_only"] is True
    assert contrast["gt_homogeneity"] is False
    assert contrast["perfect_extract"]["kind"] == "scorer_self_check"
    assert contrast["perfect_extract"]["determination_consistency_mean"] == 1.0
    assert contrast["adversarial_denied_without_reasons"]["determination_consistency"] == 0.0
    assert contrast["hub_cms_shaped"]["gt_homogeneity"] is True
    assert contrast["hub_cms_shaped"]["determination_consistency_is_quality"] is False

    compliance = packs["compliance_filing"]
    assert compliance["in_hub"] is False
    assert compliance["in_hf_pilot"] is False
    assert compliance["n"] == 2
    assert "10-K" in compliance["subclasses"]
    assert compliance["perfect_extract"]["n"] == 2
    assert compliance["perfect_extract"]["extraction_overall_mean"] is not None

    corporate = packs["corporate_extraction"]
    assert corporate["hub_extract_is_subclass_only"] is True
    assert "entity_name" in corporate["schema_fields"]
    assert "subject_matter" in corporate["schema_fields"]
    assert "signatories" in corporate["schema_fields"]
    assert corporate["perfect_extract"]["n"] == 2
    assert corporate["perfect_extract"]["extraction_overall_mean"] is not None


def test_local_packs_are_fixture_backed():
    for sample in all_local_pack_samples():
        assert sample["text"].strip()
        assert sample["expected_fields"]
        assert sample["mock_only"] is True
        assert sample["pack"] is True
    assert {s["expected_hf_class"] for s in compliance_local_samples()} == {"compliance_filing"}
    assert {s["expected_hf_class"] for s in corporate_extraction_samples()} == {"corporate_record"}


def test_local_pack_status_does_not_flip_hub_membership():
    from scripts.run_hf_pilot import HF_CLASSES, HF_HONESTY_EXCLUDED, HF_LOCAL_PACK_CLASSES

    assert "compliance_filing" not in HF_CLASSES
    assert "compliance_filing" in HF_HONESTY_EXCLUDED
    assert "compliance_filing" in HF_LOCAL_PACK_CLASSES
    status = local_pack_status("compliance_filing")
    assert status["in_hub"] is False
    assert local_pack_status("corporate_record")["hub_extract_is_subclass_only"] is True
    assert local_pack_status("corporate_record")["posthoc_schema_gt"] is True
    assert local_pack_status("insurance_claim")["hub_gt_homogeneous"] is True
    assert local_pack_status("insurance_claim")["posthoc_schema_gt"] is True
