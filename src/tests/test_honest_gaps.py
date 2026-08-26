"""v0.9.0 honesty gaps: insurance, retired court/DD, zero-row compliance, corporate_record."""

from langchain_agents.doc_inventories import CORPORATE_RECORD_TYPES
from observability.honest_gaps import (
    GAP_DOC_TYPES,
    honesty_trace_metadata,
    insurance_determination_consistent,
    insurance_determination_issues,
    suite_honesty,
)
from pipeline.config import is_extractable_doc_type, resolve_extract_class


def test_insurance_honest_gap_is_determination_consistency():
    payload = suite_honesty("insurance_claim")
    assert payload["retired"] is False
    assert payload["in_corpus"] is True
    assert "determination-consistency" in (payload["honest_gap"] or "").lower()
    assert "carrier" in payload["subclasses"]


def test_court_and_due_diligence_are_retired_from_live_suites():
    from llm_dojo_scoring import get_suite, list_suites

    court = suite_honesty("court_opinion")
    dd = suite_honesty("due_diligence")
    assert court["retired"] is True
    assert dd["retired"] is True
    assert court["in_corpus"] is False
    assert dd["in_corpus"] is False
    assert "retired" in (court["honest_gap"] or "").lower()
    assert "zero rows" in (dd["honest_gap"] or "").lower()
    live = set(list_suites(live_only=True))
    assert "court_opinions_specialist" not in live
    assert "due_diligence_specialist" not in live
    assert get_suite("court_opinion").retired is True
    assert get_suite("due_diligence").retired is True
    assert resolve_extract_class("court_opinion") is None
    assert resolve_extract_class("due_diligence") is None
    assert is_extractable_doc_type("court_opinion") is False
    assert is_extractable_doc_type("due_diligence") is False


def test_compliance_honest_gap_is_zero_hub_rows():
    payload = suite_honesty("compliance_filing")
    assert payload["retired"] is False
    assert payload["in_corpus"] is False
    gap = (payload["honest_gap"] or "").lower()
    assert "zero" in gap
    assert "10-k" in gap or "10-K" in (payload["honest_gap"] or "")
    from scripts.run_hf_pilot import HF_CLASSES, HF_HONESTY_EXCLUDED

    assert "compliance_filing" not in HF_CLASSES
    assert "compliance_filing" in HF_HONESTY_EXCLUDED


def test_corporate_record_honest_gap_is_no_external_extraction_benchmark():
    payload = suite_honesty("corporate_record")
    assert payload["in_corpus"] is True
    assert payload["retired"] is False
    assert "no external extraction benchmark" in (payload["honest_gap"] or "").lower()
    # Hub extract inventory stays five tokens; dojo sorter catalog is wider.
    assert CORPORATE_RECORD_TYPES == (
        "articles_of_incorporation",
        "bylaws",
        "powers_of_attorney",
        "rights_instrument",
        "other",
    )
    assert "certificate_of_formation" in payload["subclasses"]
    assert "bylaws" in CORPORATE_RECORD_TYPES


def test_gap_doc_types_match_v090_registry():
    assert GAP_DOC_TYPES == (
        "insurance_claim",
        "compliance_filing",
        "corporate_record",
        "court_opinion",
        "due_diligence",
    )
    for kind in GAP_DOC_TYPES:
        assert suite_honesty(kind).get("honest_gap")


def test_insurance_determination_invariant_does_not_invent_a_score():
    approved = {"coverage_determination": "approved", "denial_reasons": []}
    denied_ok = {"coverage_determination": "denied", "denial_reasons": ["exclusion"]}
    denied_bad = {"coverage_determination": "denied", "denial_reasons": []}
    approved_bad = {"coverage_determination": "approved", "denial_reasons": ["x"]}
    pending_bad = {"coverage_determination": "pending", "denial_reasons": ["x"]}
    empty = {"coverage_determination": "", "denial_reasons": []}

    assert insurance_determination_consistent(approved) is True
    assert insurance_determination_consistent(denied_ok) is True
    assert insurance_determination_consistent(denied_bad) is False
    assert insurance_determination_issues(denied_bad) == ["denied_without_reasons"]
    assert insurance_determination_consistent(approved_bad) is False
    assert insurance_determination_consistent(pending_bad) is False
    assert insurance_determination_consistent(empty) is None
    assert insurance_determination_issues({"coverage_determination": "won"}) == [
        "unknown_determination:won"
    ]

    from observability.scores import SCORE_CONFIGS

    names = {c["name"] for c in SCORE_CONFIGS}
    assert "determination_consistency" not in names
    assert "local_determination_consistent" not in names


def test_honesty_trace_metadata_is_slim_and_json_safe():
    meta = honesty_trace_metadata(
        "insurance_claim",
        {"coverage_determination": "denied", "denial_reasons": []},
    )
    assert meta["retired"] is False
    assert meta["in_corpus"] is True
    assert meta["determination_consistent"] is False
    assert "denied_without_reasons" in meta["determination_issues"]
    assert "subclasses" not in meta
    court = honesty_trace_metadata("court_opinion")
    assert court["retired"] is True
    assert court["in_corpus"] is False


def test_guard_does_not_clamp_on_determination_inconsistency():
    from pipeline.guards import apply_extraction_guard, guard_extraction

    extracted = {
        "claim_number": "C-1",
        "insurer": "Acme",
        "insured_party": "Pat",
        "claim_type": "carrier",
        "damages_description": "loss",
        "coverage_determination": "denied",
        "denial_reasons": [],
        "supporting_documents": [],
    }
    guard = guard_extraction("insurance_claim", extracted)
    assert guard["ok"] is True
    assert "denied_without_reasons" in guard["determination_issues"]
    _, conf = apply_extraction_guard("insurance_claim", extracted, 0.91, attempts=1)
    assert conf == 0.91


def test_hf_report_honesty_excludes_zero_row_compliance():
    from scripts.run_hf_pilot import hf_corpus_honesty, render_metrics_markdown

    honesty = hf_corpus_honesty()
    assert honesty["compliance_filing"]["in_hf_pilot"] is False
    assert honesty["compliance_filing"]["in_corpus"] is False
    assert honesty["corporate_record"]["in_hf_pilot"] is True
    assert honesty["court_opinion"]["retired"] is True
    md = render_metrics_markdown({"session_id": "pilot-hf-test", "samples": [], "honesty": honesty})
    assert "Corpus honesty" in md
    assert "compliance_filing" in md
    assert "no external extraction benchmark" in md.lower() or "honest gap" in md.lower()
