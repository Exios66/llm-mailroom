"""Empty-field contract guard for specialist extraction schemas.

The dojo scores against one empty-field contract
(``docs/EXTRACTION_SCHEMAS.md``): an unstated scalar is ``null``, an unstated
list is ``[]``, and correct emptiness earns full credit. The specialist
prompts teach the same contract. The pydantic extraction schemas live in
llm-mailroom, though, and nothing checked them against it.

SAND-042 is the failure this prevents: ``InsuranceClaimExtraction`` declared
``insurer`` / ``insured_party`` / ``claim_type`` / ``damages_description`` /
``coverage_determination`` as non-nullable ``str = ""``. A prompt-faithful
``null`` (and the harness's own ``"" → None`` normalizer) failed validation,
so schema-valid sat at 26–34 % on Qwen3-8B while every other class read
96–100 %. That number measured a schema bug, not the model.

:func:`contract_violations` takes any pydantic-v2 model (duck-typed — no
pydantic dependency here) and reports every scored field whose contract
empty value the model rejects. Consumers assert it is empty in CI.
"""

from __future__ import annotations

from typing import Any, Mapping

from .corpus import CORPUS_DOC_TYPES
from .suites import DEFAULT_FIELD_TYPES

__all__ = [
    "EMPTY_OVERRIDES",
    "contract_empty_payload",
    "contract_empty_value",
    "contract_violations",
]

#: Fields whose registered empty value is not ``null`` / ``[]``.
#: Correspondence ``urgency`` is never null — unspecified is ``routine``.
EMPTY_OVERRIDES: dict[str, dict[str, Any]] = {
    "correspondence": {"urgency": "routine"},
}


def contract_empty_value(doc_type: str, field: str) -> Any:
    """The value a schema must accept when *field* is unstated."""
    override = EMPTY_OVERRIDES.get(doc_type, {})
    if field in override:
        return override[field]
    field_type = DEFAULT_FIELD_TYPES[doc_type][field]
    return [] if str(field_type).startswith("entity_list") else None


def contract_empty_payload(doc_type: str) -> dict[str, Any]:
    """A fully-unstated extraction: every scored field at its empty value."""
    return {
        field: contract_empty_value(doc_type, field)
        for field in DEFAULT_FIELD_TYPES[doc_type]
    }


def _rejects(model: Any, field: str, value: Any) -> bool:
    """Whether *model* rejects *value* for *field* itself.

    Only errors located on *field* count: a model with other required fields
    fails a one-field probe for those fields too, and that must not be
    blamed on the probed value. Exceptions without pydantic's ``errors()``
    count as a rejection.
    """
    try:
        model.model_validate({field: value})
    except Exception as exc:  # pydantic.ValidationError, duck-typed
        errors = getattr(exc, "errors", None)
        if not callable(errors):
            return True
        try:
            details = errors()
        except Exception:
            return True
        return any(tuple(err.get("loc") or ())[:1] == (field,) for err in details)
    return False


def contract_violations(
    doc_type: str,
    model: Any,
    *,
    fields: Mapping[str, Any] | None = None,
) -> list[str]:
    """Scored fields whose contract empty value *model* rejects.

    Each field is probed alone (every other field left to the model's
    default), so one bad field cannot hide another; only a validation error
    on the probed field itself counts, so a missing *other* required field
    is never blamed on it. ``fields`` overrides
    individual empty values for a probe — e.g. ``{"claimed_amount": "$1,250.00"}`` to check a
    money field accepts the currency phrase the prompt allows. Returns the
    offending field names in field-map order; ``[]`` means the schema honors
    the contract.
    """
    if doc_type not in CORPUS_DOC_TYPES:
        raise KeyError(f"not a live extraction class: {doc_type!r}")
    probes = contract_empty_payload(doc_type)
    probes.update(fields or {})
    bad: list[str] = []
    for field in DEFAULT_FIELD_TYPES[doc_type]:
        if _rejects(model, field, probes[field]):
            bad.append(field)
    return bad
