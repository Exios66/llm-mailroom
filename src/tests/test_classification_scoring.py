"""Exact class match — merger_agreement is not contract."""

from observability.classification_scoring import classes_match, score_exact_classification


def test_maud_is_not_cuad():
    assert classes_match("merger_agreement", "merger_agreement") is True
    assert classes_match("merger_agreement", "contract") is False
    assert classes_match("contract", "merger_agreement") is False
    assert classes_match("insurance_claim", "insurance_claim") is True
    assert classes_match("", "contract") is False
    assert classes_match("contract", None) is False


def test_score_exact_does_not_use_dojo_align_alias():
    out = score_exact_classification(
        ["merger_agreement", "contract"],
        ["contract", "contract"],
    )
    assert out["n"] == 2
    assert out["exact_n"] == 1
    assert out["exact_accuracy"] == 0.5
    assert out["aligned_accuracy"] == 0.5
    assert out["aligned_equals_exact"] is True
    from llm_dojo_scoring.mailroom import score_aligned_classification

    aliased = score_aligned_classification(
        ["merger_agreement", "contract"],
        ["contract", "contract"],
    )
    assert aliased["aligned_accuracy"] == 1.0
    assert aliased["exact_accuracy"] == 0.5
