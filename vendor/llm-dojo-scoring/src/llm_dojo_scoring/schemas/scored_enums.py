"""Consumer-facing grammar hints for the enum fields the dojo scores (issue #96).

SAND-042 found that every consumer hand-maintained the ``intent`` values it
appended to extraction prompts, and those lists had drifted from the scored
vocabulary: the model was taught ``demand_payment`` / ``schedule_meeting``
while the canonical tokens are ``payment_demand`` / ``meeting_invite``. Those
two spellings are folded by ``normalize_intent`` before scoring, so they are not
necessarily marked wrong, but other drifted values are, and a hint must teach
the canonical vocabulary. The same defect sat in the insurance and
corporate-record hints.

The dojo owns the scored vocabulary (:mod:`.intents`, :mod:`.corpus`), so it
now owns the hints as well, the way :mod:`.maud_schema` already does for MAUD:

- :func:`scored_vocabulary` — the exact tokens the scorer credits;
- :func:`scored_enum_schema` / :func:`scored_enum_schemas` — JSON Schema
  fragments for guided decoding;
- :func:`scored_enum_hint` — a prompt-ready line;
- :func:`enum_hint_violations` — a consumer asserts its vendored hint lists
  match the dojo's instead of re-implementing the check.

Only canonical scored tokens are taught. ``insurance_claim.claim_type`` is
the product line only (:data:`.corpus.INSURANCE_PRODUCT_LINES`); since #93 the
CMS source-table tokens (``pde``, ``inpatient``, ``outpatient``, ``carrier``)
are the separate ``cms_source_table`` field and score 0 on ``claim_type``
(a CMS row's product line is ``health``). The scorer still folds intent aliases (``normalize_intent``), but a hint that
relies on that leniency is reported, because the alias table is a safety net
for stored answers, not a vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from .corpus import CMS_SOURCE_TABLES, INSURANCE_PRODUCT_LINES
from .intents import INTENT_DESCRIPTIONS, INTENT_LABELS

__all__ = [
    "SCORED_ENUM_FIELDS",
    "enum_hint_violations",
    "scored_enum_hint",
    "scored_enum_schema",
    "scored_enum_schemas",
    "scored_vocabulary",
]

_CLAIM_TYPE_DESCRIPTION = (
    "Product line of THIS claim: auto, property, liability, health, life, "
    "workers_comp, or other. A CMS / DE-SynPUF row is health; its source table "
    "(pde, inpatient, outpatient, carrier) goes in cms_source_table, not here."
)
_CMS_SOURCE_TABLE_DESCRIPTION = (
    "CMS / DE-SynPUF source table of THIS row (carrier, inpatient, outpatient, pde); "
    "null for a non-CMS claim."
)

_scored_enum_fields: dict[str, dict[str, tuple[tuple[str, ...], str]]] = {
    doc_type: {"intent": (labels, INTENT_DESCRIPTIONS[doc_type])}
    for doc_type, labels in INTENT_LABELS.items()
}
_scored_enum_fields["insurance_claim"].update(
    {
        "claim_type": (tuple(INSURANCE_PRODUCT_LINES), _CLAIM_TYPE_DESCRIPTION),
        "cms_source_table": (tuple(sorted(CMS_SOURCE_TABLES)), _CMS_SOURCE_TABLE_DESCRIPTION),
    }
)

#: doc_type -> field -> (scored vocabulary, description). Every ``label``-typed
#: field in ``DEFAULT_FIELD_TYPES`` appears here, plus the insurance enums the
#: scorer scores as the product line (``claim_type``) or the CMS table (``cms_source_table``).
#: Read-only at both levels: a mutation would silently change the vocabulary
#: for every caller in the process, so the mappings are frozen after building.
SCORED_ENUM_FIELDS: Mapping[str, Mapping[str, tuple[tuple[str, ...], str]]] = MappingProxyType(
    {doc_type: MappingProxyType(fields) for doc_type, fields in _scored_enum_fields.items()}
)
del _scored_enum_fields


def _entry(doc_type: str, field: str) -> tuple[tuple[str, ...], str]:
    """Look up one scored enum, naming the known fields on a miss."""
    fields = SCORED_ENUM_FIELDS.get(doc_type)
    if fields is None:
        raise KeyError(f"{doc_type!r} has no scored enum fields; known: {sorted(SCORED_ENUM_FIELDS)}")
    if field not in fields:
        raise KeyError(f"{doc_type}.{field} is not a scored enum; known: {sorted(fields)}")
    return fields[field]


def scored_vocabulary(doc_type: str, field: str) -> tuple[str, ...]:
    """The exact tokens the scorer credits for ``doc_type.field``, in order."""
    return _entry(doc_type, field)[0]


def scored_enum_schema(doc_type: str, field: str, *, nullable: bool = True) -> dict[str, Any]:
    """A JSON Schema fragment restricting ``field`` to its scored vocabulary.

    ``nullable`` (default) admits ``null``, the empty-field contract value for
    an unstated enum (see :mod:`.schema_contract`). The fragment carries an
    explicit ``"type"`` (``["string", "null"]`` or ``"string"``) because strict
    structured-output decoders reject an ``enum`` without one.
    """
    vocabulary, description = _entry(doc_type, field)
    values: list[Any] = list(vocabulary)
    if nullable:
        values.append(None)
    return {
        "type": ["string", "null"] if nullable else "string",
        "enum": values,
        "description": description,
    }


def scored_enum_schemas(doc_type: str, *, nullable: bool = True) -> dict[str, dict[str, Any]]:
    """Every scored enum fragment for one class, keyed by field name."""
    if doc_type not in SCORED_ENUM_FIELDS:
        return {}
    return {
        field: scored_enum_schema(doc_type, field, nullable=nullable)
        for field in SCORED_ENUM_FIELDS[doc_type]
    }


def scored_enum_hint(doc_type: str, field: str) -> str:
    """A prompt-ready line naming every scored token verbatim, then null."""
    vocabulary, description = _entry(doc_type, field)
    tokens = ", ".join(f'"{token}"' for token in vocabulary)
    return f"{field}: one of {tokens}, or null. {description}"


def enum_hint_violations(
    doc_type: str,
    vendored: Mapping[str, Iterable[Any]],
) -> list[str]:
    """Compare a consumer's vendored enum hints with the scored vocabulary.

    ``vendored`` maps a field name to the values the consumer's prompt or
    grammar allows (``None`` entries are ignored). Returns one message per
    problem, ``[]`` when the hints match:

    - a taught value that is not a scored token (the SAND-042 failure);
    - a scored token the consumer never teaches (a right answer it cannot give);
    - a vendored field that the dojo does not score as an enum;
    - a scored enum field absent from ``vendored`` (an empty mapping fails);
    - a vendored value that is neither a string nor iterable (reported, not raised).

    Matching is case-sensitive after stripping surrounding whitespace, on
    purpose: a hint must teach the canonical token, even though the scorer
    folds case. ``'AUTO'`` is reported as unscored for ``claim_type`` because
    the prompt would teach a spelling the scorer does not credit.
    """
    problems: list[str] = []
    known = SCORED_ENUM_FIELDS.get(doc_type, {})
    for field in sorted(set(known) - set(vendored)):
        problems.append(f"{doc_type}.{field}: scored enum field missing from vendored hints")
    for field in sorted(vendored):
        if field not in known:
            problems.append(f"{doc_type}.{field}: not a scored enum field")
            continue
        scored = known[field][0]
        values = vendored[field]
        if isinstance(values, (str, bytes)):
            values = [values]  # a bare string is one value, not its characters
        try:
            taught = [str(value).strip() for value in values if value is not None]
        except TypeError:  # neither a string nor iterable (e.g. an int)
            problems.append(f"{doc_type}.{field}: vendored value is not a list of values")
            continue
        unscored = sorted({value for value in taught if value not in scored})
        missing = [token for token in scored if token not in set(taught)]
        if unscored:
            problems.append(f"{doc_type}.{field}: teaches unscored values {unscored}")
        if missing:
            problems.append(f"{doc_type}.{field}: never teaches scored values {missing}")
    return problems
