"""Pin the live extraction schemas to the dojo empty-field contract (SAND-042).

The dojo scores one contract (``llm_dojo_scoring.schema_contract``): an
unstated scalar is ``null``, an unstated list is ``[]``. The specialist
prompts teach it, so a schema that rejects the contract empty value turns a
prompt-faithful answer into ``schema_valid = False`` (26-34 % on the
sandbox's SAND-042 insurance run). ``contract_violations`` must stay empty for
every live class.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from llm_dojo_scoring import contract_empty_payload, contract_violations
from llm_dojo_scoring.corpus import CORPUS_DOC_TYPES
from llm_dojo_scoring.suites import DEFAULT_FIELD_TYPES

from pipeline.config import load_config
from schemas.documents import EXTRACTION_SCHEMAS
from observability.scores import validate_extraction

LIVE_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)

# The fields the 2026-10 audit found rejecting null (SAND-042).
FORMER_VIOLATIONS: dict[str, tuple[str, ...]] = {
    "corporate_record": ("entity_name", "record_type"),
    "correspondence": ("communication_type",),
    "insurance_claim": (
        "insurer",
        "insured_party",
        "claim_type",
        "damages_description",
        "coverage_determination",
    ),
}


def test_live_classes_match_taxonomy_and_dojo():
    """Keep live document classes aligned across taxonomy, schemas, and dojo."""
    taxonomy = {c["key"] for c in load_config()["doc_classes"]}
    assert set(LIVE_DOC_TYPES) == taxonomy == set(EXTRACTION_SCHEMAS)
    assert set(LIVE_DOC_TYPES) == set(CORPUS_DOC_TYPES)


@pytest.mark.parametrize("doc_type", LIVE_DOC_TYPES)
def test_extraction_schema_has_zero_contract_violations(doc_type):
    """Require each extraction schema to satisfy the dojo empty-field contract."""
    assert contract_violations(doc_type, EXTRACTION_SCHEMAS[doc_type]) == []


@pytest.mark.parametrize("doc_type", LIVE_DOC_TYPES)
def test_every_dojo_scored_field_exists_on_the_schema(doc_type):
    """A scored field the model lacks would pass the probe vacuously
    (pydantic ignores unknown keys), hiding a real violation."""
    missing = set(DEFAULT_FIELD_TYPES[doc_type]) - set(EXTRACTION_SCHEMAS[doc_type].model_fields)
    assert missing == set()


@pytest.mark.parametrize("doc_type", LIVE_DOC_TYPES)
def test_fully_unstated_payload_is_schema_valid(doc_type):
    """Accept the canonical empty payload for every live document class."""
    payload = contract_empty_payload(doc_type)
    assert validate_extraction(doc_type, payload)["schema_valid"] is True


@pytest.mark.parametrize(
    ("doc_type", "field"),
    [(doc, field) for doc, fields in FORMER_VIOLATIONS.items() for field in fields],
)
def test_former_violations_accept_null_and_still_accept_text(doc_type, field):
    """Accept null and text for corrected scalar fields and default them to null."""
    model = EXTRACTION_SCHEMAS[doc_type]
    assert model.model_validate({field: None}).model_dump()[field] is None
    assert model.model_validate({field: "stated"}).model_dump()[field] == "stated"
    # An unstated scalar defaults to null, not "".
    assert model.model_fields[field].default is None


@pytest.mark.parametrize("doc_type", FORMER_VIOLATIONS)
@pytest.mark.parametrize("explicit_nulls", [False, True], ids=["omitted", "explicit-null"])
def test_corrected_scalars_remain_null_after_json_round_trip(doc_type, explicit_nulls):
    model = EXTRACTION_SCHEMAS[doc_type]
    fields = FORMER_VIOLATIONS[doc_type]
    payload = dict.fromkeys(fields) if explicit_nulls else {}

    extracted = model.model_validate_json(json.dumps(payload))
    serialized = json.loads(extracted.model_dump_json())
    assert {field: serialized[field] for field in fields} == dict.fromkeys(fields)
    assert model.model_validate_json(extracted.model_dump_json()) == extracted


@pytest.mark.parametrize(
    ("doc_type", "field"),
    [(doc, field) for doc, fields in FORMER_VIOLATIONS.items() for field in fields],
)
@pytest.mark.parametrize("value", ["", "Acme — café"])
def test_corrected_scalars_preserve_legacy_empty_and_unicode_strings(doc_type, field, value):
    model = EXTRACTION_SCHEMAS[doc_type]
    payload = {field: value}
    extracted = model.model_validate_json(json.dumps(payload))
    assert json.loads(extracted.model_dump_json())[field] == value
    assert validate_extraction(doc_type, payload) == {"parse_error": False, "schema_valid": True}


@pytest.mark.parametrize(
    ("doc_type", "field"),
    [(doc, field) for doc, fields in FORMER_VIOLATIONS.items() for field in fields],
)
@pytest.mark.parametrize("value", [0, False, [], {}], ids=["number", "bool", "list", "object"])
def test_nullable_scalars_still_reject_non_string_values(doc_type, field, value):
    """Accepting null must not let structured or numeric LLM output through."""
    payload = {field: value}
    with pytest.raises(ValidationError) as exc:
        EXTRACTION_SCHEMAS[doc_type].model_validate(payload)
    assert any(error["loc"] == (field,) for error in exc.value.errors())
    assert validate_extraction(doc_type, payload) == {"parse_error": False, "schema_valid": False}


@pytest.mark.parametrize(
    ("doc_type", "field"),
    [(doc, field) for doc, fields in FORMER_VIOLATIONS.items() for field in fields],
)
def test_specialist_json_schema_advertises_nullable_scalars(doc_type, field):
    """The schema sent to the LLM must describe the same empty value we accept."""
    schema = EXTRACTION_SCHEMAS[doc_type].model_json_schema()
    property_schema = schema["properties"][field]
    assert {option["type"] for option in property_schema["anyOf"]} == {"string", "null"}
    assert property_schema["default"] is None
    assert field not in schema.get("required", [])
