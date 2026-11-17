"""#285: a closed-label extraction value off its vocabulary is routed to review, not archived.

``taxonomy.yaml`` (``doc_classes[].label_vocabularies``) is the single source of truth for the
vocabularies; they match the Dojo's scored vocabulary. After extraction a non-blank value that
folds onto no token routes to ``human_review`` with cause ``out_of_vocabulary`` (never mapped
to ``other``). Case and spacing are normalised first, and #280 (a CMS table token on
``claim_type`` is ``health``) still runs before the check.
"""

from __future__ import annotations

import pytest

from graph import routing
from pipeline import reconsideration as rc
from pipeline.label_vocab import (
    canonical_label_value,
    canonicalize_labels,
    get_label_vocabularies,
    out_of_vocabulary_fields,
)

# (doc_type, field, a valid token, an off-list value, a minimal valid extraction around it)
CASES = [
    ("contract", "cuad_family", "license", "software licence", {"document_name": "MSA"}),
    ("merger_agreement", "merger_consideration", "all_cash", "cash and stock", {"document_name": "APM"}),
    ("correspondence", "communication_type", "letter", "fax", {"sender": "A", "recipient": "B"}),
    ("correspondence", "urgency", "routine", "asap", {"sender": "A", "recipient": "B"}),
    ("insurance_claim", "claim_type", "auto", "marine", {"claim_number": "C-1"}),
    ("insurance_claim", "coverage_determination", "denied", "under appeal", {"claim_number": "C-1"}),
]
_IDS = [f"{c[0]}.{c[1]}" for c in CASES]


def _state(doc_type, extracted, **extra):
    return {
        "doc_id": "d-285",
        "doc_type": doc_type,
        "extracted_data": extracted,
        "extraction_confidence": 0.97,
        "extraction_attempts": 1,
        **extra,
    }


def test_taxonomy_vocabularies_match_the_dojo_scored_vocabulary():
    from llm_dojo_scoring.schemas.scored_enums import SCORED_ENUM_FIELDS

    declared = 0
    for doc_type in ("contract", "merger_agreement", "correspondence", "insurance_claim"):
        for field, vocabulary in get_label_vocabularies(doc_type).items():
            declared += 1
            assert set(vocabulary) == set(SCORED_ENUM_FIELDS[doc_type][field][0]), (doc_type, field)
    assert declared == len(CASES)


def test_every_declared_vocabulary_is_a_label_field_of_its_class():
    from pipeline.config import get_doc_class

    for doc_type in ("contract", "merger_agreement", "correspondence", "insurance_claim"):
        types = get_doc_class(doc_type)["field_types"]
        for field in get_label_vocabularies(doc_type):
            assert types[field] == "label"


@pytest.mark.parametrize("doc_type, field, valid, off, base", CASES, ids=_IDS)
def test_off_list_value_is_found_and_valid_or_blank_is_not(doc_type, field, valid, off, base):
    assert out_of_vocabulary_fields(doc_type, {**base, field: off}) == {field: off}
    for ok in (valid, valid.upper(), f"  {valid.replace('_', ' ')}  ", None, "", "   "):
        assert out_of_vocabulary_fields(doc_type, {**base, field: ok}) == {}, repr(ok)


@pytest.mark.parametrize("doc_type, field, valid, off, base", CASES, ids=_IDS)
def test_routing_parks_an_off_list_value_with_the_cause(doc_type, field, valid, off, base):
    ok = _state(doc_type, {**base, field: valid})
    assert routing.after_extraction(ok) == "compile_report"  # the same document, on-list
    bad = _state(doc_type, {**base, field: off})
    assert routing.after_extraction(bad) == "human_review"
    assert rc.OUT_OF_VOCABULARY in rc.collect_review_causes(bad)
    assert rc.OUT_OF_VOCABULARY not in rc.collect_review_causes(ok)


@pytest.mark.parametrize("doc_type, field, valid, off, base", CASES, ids=_IDS)
def test_retry_path_applies_the_same_gate(doc_type, field, valid, off, base):
    bad = _state(doc_type, {**base, field: off})
    assert routing.after_retry_extraction(bad) == "human_review"
    assert routing.after_extraction_gated(bad) == "human_review"


def test_off_list_value_is_never_mapped_to_other():
    extracted = {"claim_number": "C-1", "claim_type": "marine"}
    assert canonicalize_labels("insurance_claim", extracted)["claim_type"] == "marine"


def test_case_and_spacing_are_normalised_to_the_canonical_token():
    out = canonicalize_labels(
        "correspondence", {"communication_type": " Press  Release ", "urgency": "Time Sensitive"}
    )
    assert out["communication_type"] == "press_release"
    assert out["urgency"] == "time-sensitive"
    assert canonical_label_value(("workers_comp", "other"), "Workers-Comp") == "workers_comp"


def test_cms_table_token_is_folded_to_health_by_the_extract_enrichment_before_the_check():
    """#280 still runs first: the table token never reaches the vocabulary check."""
    import graph.build_graph as bg

    state = {"doc_type": "insurance_claim", "doc_subclass": "inpatient"}
    enriched = bg._enrich_contract_result({"claim_number": "C-1", "claim_type": "Inpatient"}, state)
    assert enriched["claim_type"] == "health"
    assert enriched["cms_source_table"] == "inpatient"
    assert routing.after_extraction(_state("insurance_claim", enriched)) == "compile_report"


@pytest.mark.parametrize("token", ["pde", "inpatient", "outpatient", "carrier"])
def test_cms_table_token_that_bypasses_the_280_fold_is_parked(token):
    """A table token that reaches routing unfolded (enrichment skipped or failed) is off-list."""
    state = _state("insurance_claim", {"claim_number": "C-1", "claim_type": token})
    assert routing.after_extraction(state) == "human_review"
    assert rc.OUT_OF_VOCABULARY in rc.collect_review_causes(state)


def test_extract_node_canonicalises_then_routing_checks(monkeypatch):
    import graph.build_graph as bg

    def fake_extractor(doc_text, pages, handoff):
        return {"confidence": 0.97, "sender": "A", "recipient": "B",
                "communication_type": "LETTER", "urgency": "asap"}

    monkeypatch.setattr(bg, "_build_specialist_dispatch", lambda: {"correspondence": fake_extractor})
    state = {"doc_id": "d", "doc_type": "correspondence", "doc_text": "hello", "extraction_attempts": 0}
    update = bg.extract_node(state)
    assert update["extracted_data"]["communication_type"] == "letter"  # canonicalised
    assert update["extracted_data"]["urgency"] == "asap"  # off-list: left as written
    assert routing.after_extraction({**state, **update}) == "human_review"


def test_unknown_class_and_missing_extraction_have_no_vocabulary_findings():
    assert out_of_vocabulary_fields("unknown", {"claim_type": "x"}) == {}
    assert out_of_vocabulary_fields("insurance_claim", None) == {}
    assert out_of_vocabulary_fields("corporate_record", {"record_type": "anything"}) == {}


def test_cause_has_a_label():
    assert rc.OUT_OF_VOCABULARY in rc.CAUSE_LABELS
