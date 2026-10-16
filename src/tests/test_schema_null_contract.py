"""SAND-042 RCA: extraction schemas honour the empty-field contract.

The prompts, the structured-output grammar and llm-dojo-scoring all say an
unstated scalar is ``null``. Five insurance fields (and two corporate-record
fields) were ``str = ""``, so a prompt-faithful ``null`` failed validation and
insurance schema-valid read 26-34 %. These pins keep every scalar nullable and
the money fields tolerant of the currency phrases the prompts allow.
"""

import typing

import pytest
from pydantic import ValidationError

from schemas.documents import (
    EXTRACTION_SCHEMAS,
    CorrespondenceExtraction,
    InsuranceClaimExtraction,
)


def _scalar_fields(model):
    for name, field in model.model_fields.items():
        origin = typing.get_origin(field.annotation)
        if origin is list or field.annotation is list:
            continue
        if name == "confidence":
            continue
        yield name


@pytest.mark.parametrize("doc_class", sorted(EXTRACTION_SCHEMAS))
def test_every_scalar_field_accepts_null(doc_class):
    model = EXTRACTION_SCHEMAS[doc_class]
    rejected = []
    for name in _scalar_fields(model):
        try:
            model.model_validate({name: None})
        except ValidationError:
            rejected.append(name)
    assert not rejected, f"{doc_class}: null rejected by {rejected}"


def test_all_null_insurance_extraction_validates():
    payload = {name: None for name in _scalar_fields(InsuranceClaimExtraction)}
    InsuranceClaimExtraction.model_validate(payload)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$18,530.00", 18530.0),
        ("1,250", 1250.0),
        ("USD 99.5", 99.5),
        ("($40.00)", -40.0),
        ("", None),
        (1200, 1200.0),
        (None, None),
    ],
)
def test_money_fields_accept_currency_phrases(raw, expected):
    claim = InsuranceClaimExtraction.model_validate({"claimed_amount": raw})
    letter = CorrespondenceExtraction.model_validate({"demand_amount": raw})
    assert claim.claimed_amount == expected
    assert letter.demand_amount == expected


def test_money_fields_still_reject_prose():
    with pytest.raises(ValidationError):
        InsuranceClaimExtraction.model_validate({"claimed_amount": "about five grand"})
    with pytest.raises(ValidationError):
        CorrespondenceExtraction.model_validate({"demand_amount": "about five grand"})
