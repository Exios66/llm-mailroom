"""Pure decision-model review routing."""

import pytest

from pipeline.decision_review import DecisionAnswer, ReviewRoute, decide_review
from pipeline.reconsideration import (
    CAUSE_LABELS,
    REVIEWER_DISAGREEMENT,
    REVIEWER_INELIGIBLE_LABEL,
    REVIEWER_LOW_CONFIDENCE,
    REVIEWER_NO_TEXT,
    REVIEWER_UNAVAILABLE,
)

ELIGIBLE = {"insurance_claim", "corporate_record"}
CUTOFFS = {"insurance_claim": 0.9, "corporate_record": 0.9}


def route(sorter="insurance_claim", label="insurance_claim", conf=0.95, text=True):
    answer = DecisionAnswer(label, None, conf, "jev-1.13.0")
    return decide_review(sorter, answer, eligible=ELIGIBLE, cutoffs=CUTOFFS, has_text=text)


def test_clean_accept():
    r = route()
    assert r.destination == "extract"
    assert r.causes == ()
    assert r.verdict == "decision_accepts"


def test_records_every_failed_condition():
    r = route(label="contract", conf=0.1)
    assert r.destination == "human_review"
    assert r.verdict == "decision_escalates"
    assert set(r.causes) == {
        REVIEWER_INELIGIBLE_LABEL,
        REVIEWER_DISAGREEMENT,
        REVIEWER_LOW_CONFIDENCE,
    }


def test_causes_follow_table_order():
    r = route(label="contract", conf=0.1, text=False)
    assert r.causes == (
        REVIEWER_NO_TEXT,
        REVIEWER_INELIGIBLE_LABEL,
        REVIEWER_DISAGREEMENT,
        REVIEWER_LOW_CONFIDENCE,
    )


def test_no_answer_is_unavailable():
    r = decide_review("insurance_claim", None, eligible=ELIGIBLE, cutoffs=CUTOFFS, has_text=True)
    assert r.destination == "human_review"
    assert r.causes == (REVIEWER_UNAVAILABLE,)


def test_no_answer_and_no_text():
    r = decide_review("insurance_claim", None, eligible=ELIGIBLE, cutoffs=CUTOFFS, has_text=False)
    assert r.causes == (REVIEWER_UNAVAILABLE, REVIEWER_NO_TEXT)


def test_override_never_auto_accepts():
    r = route(sorter="corporate_record", label="insurance_claim", conf=1.0)
    assert r.destination == "human_review"
    assert r.causes == (REVIEWER_DISAGREEMENT,)


def test_label_outside_taxonomy_parks():
    r = route(label="made_up_class")
    assert r.destination == "human_review"
    assert REVIEWER_INELIGIBLE_LABEL in r.causes


def test_ineligible_sorter_label_parks():
    r = route(sorter="contract", label="contract")
    assert r.destination == "human_review"
    assert REVIEWER_INELIGIBLE_LABEL in r.causes


@pytest.mark.parametrize("conf", [float("nan"), float("inf"), -0.1, 1.1, None, 10**1000, -(10**1000)])
def test_invalid_confidence_parks(conf):
    r = route(conf=conf)
    assert r.destination == "human_review"
    assert REVIEWER_LOW_CONFIDENCE in r.causes


def test_confidence_at_cutoff_accepts():
    assert route(conf=0.9).destination == "extract"


def test_missing_cutoff_parks():
    r = decide_review(
        "insurance_claim",
        DecisionAnswer("insurance_claim", None, 1.0, "m"),
        eligible=ELIGIBLE,
        cutoffs={},
        has_text=True,
    )
    assert r.causes == (REVIEWER_LOW_CONFIDENCE,)


def test_no_text_is_recorded():
    r = route(text=False)
    assert r.destination == "human_review"
    assert REVIEWER_NO_TEXT in r.causes


def test_missing_sorter_label_disagrees():
    r = route(sorter=None)
    assert REVIEWER_DISAGREEMENT in r.causes


def test_route_is_frozen():
    with pytest.raises(Exception):
        route().destination = "human_review"  # type: ignore[misc]
    assert isinstance(route(), ReviewRoute)


def test_every_cause_has_a_label():
    for cause in (
        REVIEWER_UNAVAILABLE,
        REVIEWER_NO_TEXT,
        REVIEWER_INELIGIBLE_LABEL,
        REVIEWER_DISAGREEMENT,
        REVIEWER_LOW_CONFIDENCE,
    ):
        assert CAUSE_LABELS[cause]


def test_oversized_cutoff_parks():
    r = decide_review(
        "insurance_claim", DecisionAnswer("insurance_claim", None, 1.0, "m"),
        eligible=ELIGIBLE, cutoffs={"insurance_claim": 10**1000}, has_text=True,
    )
    assert r.causes == (REVIEWER_LOW_CONFIDENCE,)
    assert "no valid cutoff" in CAUSE_LABELS[REVIEWER_LOW_CONFIDENCE]
