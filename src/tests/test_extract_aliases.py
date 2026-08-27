"""Live taxonomy class: merger_agreement (MAUD) is not an alias of contract (CUAD)."""

from pipeline.config import (
    EXTRACT_CLASS_ALIASES,
    get_all_doc_types,
    get_sorter_label_set,
    is_extractable_doc_type,
    resolve_extract_class,
)
from schemas.documents import EXTRACTION_SCHEMAS, ContractExtraction, get_extraction_schema


def test_no_extract_alias_collapses_maud_into_cuad():
    assert EXTRACT_CLASS_ALIASES == {}
    assert "court_opinion" not in EXTRACT_CLASS_ALIASES
    assert "due_diligence" not in EXTRACT_CLASS_ALIASES


def test_resolve_extract_class():
    assert resolve_extract_class("contract") == "contract"
    assert resolve_extract_class("merger_agreement") == "merger_agreement"
    assert resolve_extract_class("insurance_claim") == "insurance_claim"
    assert resolve_extract_class("unknown") is None
    assert resolve_extract_class("court_opinion") is None
    assert resolve_extract_class("due_diligence") is None
    assert resolve_extract_class("") is None
    assert resolve_extract_class(None) is None


def test_is_extractable_includes_merger_not_retired():
    assert is_extractable_doc_type("merger_agreement") is True
    assert is_extractable_doc_type("contract") is True
    assert is_extractable_doc_type("unknown") is False
    assert is_extractable_doc_type("court_opinion") is False
    assert "merger_agreement" in get_all_doc_types()
    assert "contract" in get_all_doc_types()


def test_sorter_label_set_includes_merger_and_unknown():
    labels = get_sorter_label_set()
    assert "merger_agreement" in labels
    assert "unknown" in labels
    for key in get_all_doc_types():
        assert key in labels
    assert "court_opinion" not in labels
    assert "due_diligence" not in labels


def test_extraction_schema_registers_merger_as_own_class():
    assert get_extraction_schema("merger_agreement") is ContractExtraction
    assert get_extraction_schema("contract") is ContractExtraction
    assert "merger_agreement" in EXTRACTION_SCHEMAS
    assert len(EXTRACTION_SCHEMAS) == 6
