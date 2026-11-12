"""The document-class registry: the one table a person edits to change a class or a field.

Every other class list and every per-class field map in the package derives
from ``DOC_CLASSES`` below (``scoring.suites.DEFAULT_FIELD_TYPES``,
``schemas.corpus.CORPUS_EXTRACTION_FIELDS``, the live and retired rosters, the
scope and adherence field sets, the prompt descriptors). To change the schema:

1. **Add or retype a field**: edit that class's ``fields`` (``name -> field type``, in
   scoring order). Types: ``name``, ``label``, ``date``, ``money``, ``id``,
   ``free_text``, ``entity_list`` or ``entity_list:<name|free_text|id>``.
2. **Add a class**: append one ``DocClass`` (``live``, ``specialist``, ``in_corpus``,
   ``fields``). The derived tables append it. A live class also needs a
   ``DEFAULT_PROFILES`` entry in ``metrics/profiles.py``: ``DEFAULT_SUITES`` is built from
   it, so ``get_suite(<class>)`` raises ``KeyError`` without one (see
   ``docs/guides/ADDING_A_DOCUMENT_TYPE.md``).
3. **Contract and merger fields** also need a prompt descriptor in
   ``schemas/cuad_schema.py`` / ``schemas/maud_schema.py``; the import fails naming the
   missing field until it exists.
4. **When the dataset labels the field**, re-run ``scripts/build_extraction_status.py
   --from-hub …`` and ``scripts/render_gt_coverage.py --write``; whether a field is
   reported as scoreable comes from that file (``extraction_schemas.scoreable_fields``,
   a status report, not a scoring gate), not from here.
5. A score that moves needs ``scripts/freeze_goldens.py --write --reason …`` and a
   ``CHANGELOG.md`` entry.

This module imports nothing from the package (it is the leaf the others import) and
reads ``DOC_CLASSES`` at call time, so a test can swap an entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "DocClass",
    "DOC_CLASSES",
    "LIVE_DOC_TYPES",
    "RETIRED_DOC_TYPES",
    "ROSTER_DOC_TYPES",
    "field_names",
    "field_types",
    "in_order",
]


@dataclass(frozen=True)
class DocClass:
    """One mailroom document class and its extraction fields.

    ``fields`` maps field name to scorer field type, in scoring order; it is frozen
    on construction. ``specialist`` is the extraction agent that handles the class;
    its auditor is :attr:`auditor`. ``live`` marks a class the pipeline extracts
    today (the rest are retired but still scoreable for historical traces).
    ``in_corpus`` says the pinned mailroom-dataset has ground-truth rows for it; it
    is a separate fact from ``live`` (a new class can be live before it is labelled).
    """

    key: str
    live: bool
    specialist: str
    fields: Mapping[str, str]
    in_corpus: bool

    def __post_init__(self) -> None:
        """Freeze the field map so the registry cannot be mutated in place."""
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def auditor(self) -> str:
        """The class's auditor agent name (``<stem>_specialist`` -> ``<stem>_auditor``).

        ``contracts_specialist`` is audited by ``contract_auditor`` (the taxonomy's name);
        ``merger_agreement`` has no auditor agent, so its derived name has no profile.
        """
        return _AUDITOR_OVERRIDES.get(self.key) or self.specialist.removesuffix("_specialist") + "_auditor"


#: Auditor names that do not follow the ``<stem>_specialist`` -> ``<stem>_auditor`` rule.
_AUDITOR_OVERRIDES: Mapping[str, str] = MappingProxyType({"contract": "contract_auditor"})


#: Registry order is the taxonomy order the field maps have always had (the sorter's
#: seven labels, then ``merger_agreement``). Other tables keep their own legacy order
#: through :func:`in_order`; a new class is appended last in all of them.
DOC_CLASSES: Mapping[str, DocClass] = MappingProxyType(
    {
        c.key: c
        for c in (
            # ContractExtraction v2 (0.28.0, mailroom-dataset v10). The v1 parse-compat keys
            # ``merger_consideration`` / ``maud_clauses`` are accepted but never scored, so
            # they are not fields here (``cuad_schema.CONTRACT_PARSE_COMPAT_FIELDS``).
            DocClass(
                key="contract",
                live=True,
                specialist="contracts_specialist",
                in_corpus=True,
                fields={
                    "document_name": "name",
                    "parties": "entity_list:name",
                    "party_roles": "entity_list:free_text",
                    "signatories": "entity_list:name",
                    "agreement_date": "date",
                    "effective_date": "date",
                    "expiration_date": "date",
                    "term_length": "free_text",
                    "renewal_terms": "free_text",
                    "termination_notice_period": "free_text",
                    "governing_law": "name",
                    "dispute_forum": "name",
                    "contract_value": "money",
                    "payment_terms": "free_text",
                    "territory": "free_text",
                    "cuad_family": "label",
                    "cuad_clauses": "entity_list:free_text",
                },
            ),
            DocClass(
                key="corporate_record",
                live=True,
                specialist="corporate_records_specialist",
                in_corpus=True,
                fields={
                    "entity_name": "name",
                    "record_type": "name",
                    "effective_date": "date",
                    "signatories": "entity_list:name",
                    "jurisdiction": "name",
                    "filing_number": "id",
                    "intent": "label",
                    "subject_matter": "free_text",
                    "keywords": "entity_list:name",
                },
            ),
            DocClass(
                key="due_diligence",
                live=False,
                specialist="due_diligence_specialist",
                in_corpus=False,
                fields={
                    "target_entity": "name",
                    "diligence_type": "name",
                    "material_findings": "entity_list:free_text",
                    "risk_flags": "entity_list:free_text",
                    "outstanding_items": "entity_list:free_text",
                    "document_date": "date",
                    "prepared_by": "name",
                },
            ),
            DocClass(
                key="correspondence",
                live=True,
                specialist="correspondence_specialist",
                in_corpus=True,
                fields={
                    "sender": "name",
                    "recipient": "name",
                    "additional_recipients": "entity_list",
                    "communication_type": "label",
                    "communication_date": "date",
                    "demand_amount": "money",
                    "action_items": "entity_list",
                    "urgency": "label",
                    "intent": "label",
                    "subject_matter": "free_text",
                    "keywords": "entity_list:name",
                },
            ),
            DocClass(
                key="compliance_filing",
                live=False,
                specialist="compliance_specialist",
                in_corpus=False,
                fields={
                    "filing_type": "name",
                    "regulatory_body": "name",
                    "filing_date": "date",
                    "due_date": "date",
                    "entity_name": "name",
                    "key_requirements": "entity_list:free_text",
                    "status": "name",
                    "reference_number": "id",
                },
            ),
            DocClass(
                key="court_opinion",
                live=False,
                specialist="court_opinions_specialist",
                in_corpus=False,
                fields={
                    "case_name": "name",
                    "court": "name",
                    "date_decided": "date",
                    "docket_number": "id",
                    "opinion_type": "name",
                    "parties": "entity_list:name",
                    "holding": "free_text",
                    "legal_issues": "entity_list:free_text",
                    "outcome": "free_text",
                    "citations": "entity_list:id",
                    "authored_by": "name",
                },
            ),
            # ``claim_checklist`` has no ground truth yet: it stays a field (the pipeline
            # emits it) and is reported ``pending_annotation``, never scored.
            DocClass(
                key="insurance_claim",
                live=True,
                specialist="insurance_claims_specialist",
                in_corpus=True,
                fields={
                    "claim_number": "id",
                    "policy_number": "id",
                    "insurer": "name",
                    "insured_party": "name",
                    "claim_type": "label",
                    "date_of_loss": "date",
                    "date_filed": "date",
                    "claimed_amount": "money",
                    "adjuster": "name",
                    "damages_description": "free_text",
                    "coverage_determination": "label",
                    "denial_reasons": "entity_list:free_text",
                    "supporting_documents": "entity_list",
                    "intent": "label",
                    "subject_matter": "free_text",
                    "keywords": "entity_list:name",
                    "claim_checklist": "entity_list:free_text",
                },
            ),
            # MergerAgreementExtraction v2 (0.28.0, mailroom-dataset v10).
            DocClass(
                key="merger_agreement",
                live=True,
                specialist="merger_agreement_specialist",
                in_corpus=True,
                fields={
                    "document_name": "name",
                    "parties": "entity_list:name",
                    "acquirer": "name",
                    "target": "name",
                    "merger_sub": "entity_list:name",
                    "signatories": "entity_list:name",
                    "effective_date": "date",
                    "effective_time": "free_text",
                    "governing_law": "name",
                    "per_share_cash_amount": "money",
                    "exchange_ratio": "free_text",
                    "target_termination_fee": "money",
                    "parent_termination_fee": "money",
                    "outside_date": "date",
                    "financial_advisors": "entity_list:name",
                    "legal_counsel": "entity_list:name",
                    "merger_consideration": "label",
                    "maud_clauses": "entity_list:free_text",
                    "intent": "label",
                    "subject_matter": "free_text",
                    "keywords": "entity_list:name",
                },
            ),
        )
    }
)


def in_order(keys: Iterable[str], first: Sequence[str]) -> tuple[str, ...]:
    """``keys`` with those named in ``first`` moved to the front, in ``first`` order.

    The keys not named keep their given order after them, so a class added to the
    registry appears at the end of a table that pins a legacy order. A name in
    ``first`` that is not a registered class raises ``KeyError`` (a rename or typo).
    """
    unknown = [name for name in first if name not in DOC_CLASSES]
    if unknown:
        raise KeyError(f"order hint names unregistered classes {unknown}; known: {list(DOC_CLASSES)}")
    pool = list(dict.fromkeys(keys))
    head = [name for name in first if name in pool]
    return tuple(head + [name for name in pool if name not in head])


def _unknown(doc_type: str) -> KeyError:
    """The error for a class that is not registered, naming the ones that are."""
    return KeyError(f"unknown document class {doc_type!r}; known: {list(DOC_CLASSES)}")


def field_types(doc_type: str) -> dict[str, str]:
    """A fresh ``{field: field type}`` dict for ``doc_type``, in scoring order."""
    try:
        return dict(DOC_CLASSES[doc_type].fields)
    except KeyError:
        raise _unknown(doc_type) from None


def field_names(doc_type: str) -> tuple[str, ...]:
    """The extraction field names of ``doc_type``, in scoring order."""
    try:
        return tuple(DOC_CLASSES[doc_type].fields)
    except KeyError:
        raise _unknown(doc_type) from None


#: Pipeline order: the order the sorter, the live specialists and the archive list the
#: five live classes, then the retired ones as they were retired.
_PIPELINE_ORDER = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
    "court_opinion",
    "due_diligence",
    "compliance_filing",
)

#: Specialist-roster order (``SPECIALIST_DOC_TYPES``, ``CORPUS_EXTRACTION_FIELDS``).
_ROSTER_ORDER = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "due_diligence",
    "correspondence",
    "compliance_filing",
    "court_opinion",
    "insurance_claim",
)

#: Classes the pipeline extracts today, in pipeline order.
LIVE_DOC_TYPES: tuple[str, ...] = in_order((k for k, c in DOC_CLASSES.items() if c.live), _PIPELINE_ORDER)

#: Classes retired from the live pipeline, in the order they were retired.
RETIRED_DOC_TYPES: tuple[str, ...] = in_order((k for k, c in DOC_CLASSES.items() if not c.live), _PIPELINE_ORDER)

#: Every class in specialist-roster order.
ROSTER_DOC_TYPES: tuple[str, ...] = in_order(DOC_CLASSES, _ROSTER_ORDER)
