"""mailroom-dataset corpus alignment — schemas, subclasses, differentiators.

Grounded in the published Hugging Face dataset
``Lucius-Morningstar/mailroom-dataset`` (default + ``ground_truth`` configs;
3,302 rows: 2,979 train / 323 test; current pin Hub tag ``v9.3`` (``ccd040f6``,
what llm-mailroom pins and ``schemas/data/extraction_status.json`` is frozen from;
the same rows as the v9 GT-closure revision ``46a4d3c2``, v9.1 ``ed7576b6``) — the v9 successor of the frozen v8 ``mailroom-corpus``
baseline).

**v10 (0.28.0 aligns to it; not yet published).** The v10 revision built in
``Exios66/Mailroom-Corpus-EDA`` keeps 3,292 rows (train 2,971 / test 321, 151
mergers) and adds typed per-class configs (``gt_contract``, ``gt_merger_agreement``,
``gt_correspondence``, ``gt_insurance_claim``, ``gt_corporate_record``), a blind
``default`` (``document_id``, ``doc_text``, ``prompt``), ``routing``, and the long
``cuad_clauses`` / ``maud_answers`` tables; the legacy ``ground_truth`` config stays,
deprecated. The new configs carry ``subclass`` as the canonical key from
:data:`DOC_TYPE_SUBCLASSES` (``source_subclass`` keeps the raw value), so the
canonical keys are corpus surfaces too. :mod:`llm_dojo_scoring.schemas.v10_rows`
maps a typed row onto the legacy ``gt_fields`` shape. The v10 tag and its commit
are pinned here when the revision is published; until then ``v9.3`` is the pin.

This module is the single source mapping each mailroom document class to:

- the **subclass taxonomy** the sorter must emit for that class,
- the **extraction fields** the specialist suite scores,
- the **corpus differentiators** (GT columns that vary by type),
- an honest note when a native mailroom class has **zero rows** in the
  published merge.

Do not invent field names that specialists do not extract. Classification
subclasses (CMS source table, CUAD family, MAUD consideration, Enron form)
live here even when they are not extraction-schema fields.
"""

from __future__ import annotations

from typing import Any

from ..config import CONTRACT_SUBTYPE_KEYS, MAUD_CONSIDERATION_TYPES, SUBTYPE_UNKNOWN
from .doc_classes import DOC_CLASSES, ROSTER_DOC_TYPES, field_names, in_order
from .equivalences import (
    equivalent_doc_subclasses,
    equivalent_subtypes,
    normalize_doc_subclass,
    normalize_subtype,
)

__all__ = [
    "CORPUS_ID",
    "CORPUS_DOC_TYPES",
    "CORPUS_ABSENT_DOC_TYPES",
    "NATIVE_DOC_TYPES",
    "DOC_TYPE_SUBCLASSES",
    "CORPUS_SUBCLASS_SURFACES",
    "RAW_CORPUS_SUBCLASS_SURFACES",
    "CORPUS_DIFFERENTIATORS",
    "CORPUS_EXTRACTION_FIELDS",
    "CUAD_CLAUSE_CATEGORIES",
    "MAUD_QUESTION_KEYS",
    "MAUD_CLAUSE_CATEGORIES",
    "CORRESPONDENCE_TOPICS",
    "CORRESPONDENCE_SENTIMENT_LABELS",
    "INSURANCE_CLAIM_TYPES",
    "COVERAGE_DETERMINATIONS",
    "URGENCY_LEVELS",
    "INSURANCE_PRODUCT_LINES",
    "CMS_DOCUMENTED_ABSENT_FIELDS",
    "CMS_SOURCE_TABLES",
    "cms_source_table",
    "normalize_corpus_subclass",
    "subclass_equivalent",
    "suite_schema",
]

#: Hugging Face dataset id this module is pinned to.
CORPUS_ID = "Lucius-Morningstar/mailroom-dataset"

#: Legacy order of the corpus class tuples (corpus-present, then corpus-absent).
#: A class added to the registry is appended after these.
_CORPUS_ORDER = (
    "contract",
    "insurance_claim",
    "merger_agreement",
    "correspondence",
    "corporate_record",
    "due_diligence",
    "compliance_filing",
    "court_opinion",
)

#: Doc types that have at least one ground-truth row in the published merge
#: (``DocClass.in_corpus`` in ``schemas/doc_classes.py``).
CORPUS_DOC_TYPES: tuple[str, ...] = in_order((k for k, c in DOC_CLASSES.items() if c.in_corpus), _CORPUS_ORDER)

#: Native mailroom classes with no rows in the published merge.
CORPUS_ABSENT_DOC_TYPES: tuple[str, ...] = in_order(
    (k for k, c in DOC_CLASSES.items() if not c.in_corpus), _CORPUS_ORDER
)

#: Full native taxonomy (corpus-present ∪ corpus-absent).
NATIVE_DOC_TYPES: tuple[str, ...] = CORPUS_DOC_TYPES + CORPUS_ABSENT_DOC_TYPES

#: Canonical subclass keys per doc type. Empty tuple = no subclass dimension
#: in the published merge (and no taxonomy-level dimension yet).
DOC_TYPE_SUBCLASSES: dict[str, tuple[str, ...]] = {
    # CUAD 25-family (canonical snake_case). Corpus surfaces are folder-style
    # labels (``License_Agreements``, ``Joint Venture _ Filing``) and normalize
    # through :func:`normalize_subtype`.
    "contract": tuple(CONTRACT_SUBTYPE_KEYS),
    # MAUD Type of Consideration (expert GT). Corpus already ships canonical keys.
    "merger_agreement": tuple(MAUD_CONSIDERATION_TYPES),
    # Content-detected record type (entity-extraction taxonomy). Corpus
    # currently covers a subset; the full enum is the scoring surface.
    "corporate_record": (
        "bylaws",
        "articles_of_incorporation",
        "certificate_of_formation",
        "charter_amendment",
        "powers_of_attorney",
        "subsidiary_list",
        "rights_instrument",
        "indenture",
        "board_resolution",
        "officer_certificate",
        "other",
    ),
    # Communication form, aligned to the CURRENT dataset GT: mailroom-dataset
    # v9 ``ground_truth.expected_subclass`` (tip 46a4d3c2) carries exactly
    # these 8 tokens + the other-bucket. The v8-era `voicemail` key from the
    # Enron eval-environment labeler enum is NOT in the v9 GT vocabulary and
    # was removed here (DMR-071 adjudication 2026-09-16: the dataset pin is
    # the source of truth). The labeler keeps its voicemail key locally —
    # text-only Enron corpora make it 0% by construction — so the two enums
    # intentionally differ by that one labeler-local key.
    "correspondence": (
        "email",
        "memo",
        "letter",
        "notice",
        "demand",
        "attorney_demand",
        "press_release",
        "meeting_request",
        "other",
    ),
    # Insurance claim-document subclass: CMS DE-SynPUF *source table* tokens
    # (carrier/inpatient/outpatient/pde) PLUS the v8 synthetic LOB lines
    # (property = GNOTHEIA FNOL bundles, auto = BDR motor decision letters;
    # HUB-028/HUB-041). The split is 1,100 rows: 600 CMS (150 per table) and
    # 500 non-CMS (INSURBIAS auto 150, BDR auto 150, GNOTHEIA property 200).
    # The table is NOT a ``claim_type``; it is scored as ``cms_source_table``.
    "insurance_claim": (
        "carrier",
        "inpatient",
        "outpatient",
        "pde",
        "property",
        "auto",
    ),
    "due_diligence": (),
    "compliance_filing": (
        "10-K",
        "10-Q",
        "8-K",
        "S-1",
        "DEF 14A",
        "13D",
        "13G",
        "Form 4",
        "20-F",
        "6-K",
        "other",
    ),
    "court_opinion": (),
}

#: Observed subclass *surfaces* in mailroom-dataset ``ground_truth.expected_subclass``.
#: Used by tests to pin corpus coverage; normalizers must resolve every value
#: to a key in :data:`DOC_TYPE_SUBCLASSES` (or ``other`` only when the
#: surface is the canonical other-bucket).
_RAW_SUBCLASS_SURFACES: dict[str, tuple[str, ...]] = {
    "contract": (
        "Affiliate_Agreements",
        "Agency Agreements",
        "Co_Branding",
        "Collaboration",
        "Consulting Agreements",
        "Development",
        "Distributor",
        "Endorsement",
        "Franchise",
        "Hosting",
        "IP",
        "Joint Venture",
        "Joint Venture _ Filing",
        "License_Agreements",
        "Maintenance",
        "Manufacturing",
        "Marketing",
        "Non_Compete_Non_Solicit",
        "Outsourcing",
        "Promotion",
        "Reseller",
        "Service",
        "Sponsorship",
        "Strategic Alliance",
        "Supply",
        "Transportation",
    ),
    "merger_agreement": (
        "all_cash",
        "all_stock",
        "mixed_cash_stock",
        "mixed_cash_stock_election",
        "other",
    ),
    "corporate_record": (
        "articles_of_incorporation",
        "board_resolution",
        "bylaws",
        "charter_amendment",
        "indenture",
        "officer_certificate",
        "other",
        "powers_of_attorney",
        "rights_instrument",
        "subsidiary_list",
    ),
    "correspondence": (
        "attorney_demand",
        "demand",
        "email",
        "letter",
        "meeting_request",
        "memo",
        "notice",
        "press_release",
    ),
    "insurance_claim": (
        "auto",
        "carrier",
        "inpatient",
        "outpatient",
        "pde",
        "property",
    ),
}

#: v10 configs carry the canonical key as ``subclass`` (``source_subclass`` keeps the
#: raw surface above), so every canonical key is a surface as well.
CORPUS_SUBCLASS_SURFACES: dict[str, tuple[str, ...]] = {
    doc_type: raw + tuple(k for k in DOC_TYPE_SUBCLASSES[doc_type] if k not in raw)
    for doc_type, raw in _RAW_SUBCLASS_SURFACES.items()
}

#: The raw (v9 ``expected_subclass``) surfaces only, without the v10 canonical keys.
RAW_CORPUS_SUBCLASS_SURFACES: dict[str, tuple[str, ...]] = dict(_RAW_SUBCLASS_SURFACES)

#: Ground-truth columns that actually vary by document type in the merge.
#: Classification subclasses are listed even when they are not extraction fields.
CORPUS_DIFFERENTIATORS: dict[str, tuple[str, ...]] = {
    "contract": ("expected_subclass", "cuad_clause_labels"),
    "merger_agreement": ("expected_subclass", "maud_clause_labels"),
    "insurance_claim": (
        "expected_subclass",
        "claim_number",
        "policy_number",
        "insurer",
        "insured_party",
        "claim_type",
        "date_of_loss",
        "date_filed",
        "claimed_amount",
        "damages_description",
        "coverage_determination",
        "supporting_documents",
    ),
    "correspondence": (
        "expected_subclass",
        "content_topic",
        "topic_evidence",
        "sentiment_label",
        "sentiment_score",
        "sentiment_evidence",
        "label_evidence",
    ),
    "corporate_record": ("expected_subclass",),
    "due_diligence": (),
    "compliance_filing": ("expected_subclass",),
    "court_opinion": (),
}

#: Extraction-schema fields each specialist suite must score, aligned to
#: live mailroom ``EXTRACTION_SCHEMAS`` + taxonomy ``field_types``, derived from the
#: document-class registry (``schemas/doc_classes.py``: edit the field there, not
#: here), in specialist-roster order. Merger is ``MergerAgreementExtraction`` (no
#: CUAD family/clauses); ``document_name`` is on the contracts / merger schema.
CORPUS_EXTRACTION_FIELDS: dict[str, tuple[str, ...]] = {key: field_names(key) for key in ROSTER_DOC_TYPES}

#: CUAD v1 clause-category names as they appear in ``cuad_clause_labels``.
CUAD_CLAUSE_CATEGORIES: tuple[str, ...] = (
    "Affiliate License-Licensee",
    "Affiliate License-Licensor",
    "Agreement Date",
    "Anti-Assignment",
    "Audit Rights",
    "Cap On Liability",
    "Change Of Control",
    "Competitive Restriction Exception",
    "Covenant Not To Sue",
    "Document Name",
    "Effective Date",
    "Exclusivity",
    "Expiration Date",
    "Governing Law",
    "Insurance",
    "Ip Ownership Assignment",
    "Irrevocable Or Perpetual License",
    "Joint Ip Ownership",
    "License Grant",
    "Liquidated Damages",
    "Minimum Commitment",
    "Most Favored Nation",
    "No-Solicit Of Customers",
    "No-Solicit Of Employees",
    "Non-Compete",
    "Non-Disparagement",
    "Non-Transferable License",
    "Notice Period To Terminate Renewal",
    "Parties",
    "Post-Termination Services",
    "Price Restrictions",
    "Renewal Term",
    "Revenue/Profit Sharing",
    "Rofr/Rofo/Rofn",
    "Source Code Escrow",
    "Termination For Convenience",
    "Third Party Beneficiary",
    "Uncapped Liability",
    "Unlimited/All-You-Can-Eat-License",
    "Volume Restriction",
    "Warranty Duration",
)

#: MAUD question keys observed in ``maud_clause_labels`` (union across rows).
MAUD_QUESTION_KEYS: tuple[str, ...] = (
    "Absence of Litigation Closing Condition",
    "Accuracy of Target R&W Closing Condition",
    "Agreement provides for matching rights in connection with COR",
    "Agreement provides for matching rights in connection with FTR",
    "Breach of Meeting Covenant",
    "Breach of No Shop",
    "Compliance with Covenant Closing Condition",
    "FTR Triggers",
    "Fiduciary exception to COR covenant",
    "Fiduciary exception:  Board determination (no-shop)",
    "General Antitrust Efforts Standard",
    "Intervening Event Definition",
    "Knowledge Definition",
    "Limitations on FTR Exercise",
    "MAE Definition",
    "Negative interim operating covenant",
    "No-Shop",
    "Ordinary course covenant",
    "Specific Performance",
    "Superior Offer Definition",
    "Tail Period & Acquisition Proposal Details",
    "Type of Consideration",
)

MAUD_CLAUSE_CATEGORIES: tuple[str, ...] = (
    "Conditions to Closing",
    "Deal Protection and Related Provisions",
    "General Information",
    "Knowledge",
    "Material Adverse Effect",
    "Operating and Efforts Covenant",
    "Remedies",
)

CORRESPONDENCE_TOPICS: tuple[str, ...] = (
    "announcements",
    "energy_market",
    "finance_earnings",
    "general_business",
    "hr_personnel",
    "it_systems",
    "legal_contracts",
    "marketing_clients",
    "regulatory",
    "scheduling",
    "travel_logistics",
)

#: Enron ``sentiment_label`` catalog (1,000/1,000 correspondence rows populated
#: at the v9 pin ``46a4d3c2``: neutral 566 / positive 254 / negative 180).
CORRESPONDENCE_SENTIMENT_LABELS: tuple[str, ...] = (
    "negative",
    "neutral",
    "positive",
)

#: Product-line ``claim_type`` vocabulary the scorer credits (dojo #93). The
#: published Hub GT carries ``claim_type`` on all 1,100 ``insurance_claim``
#: rows: ``health`` x600 (every CMS row), ``auto`` x300 (INSURBIAS 150 + BDR 150)
#: and ``property`` x200 (GNOTHEIA); ``liability`` / ``life`` / ``workers_comp``
#: / ``other`` are legacy FNOL lines no GT row uses yet.
#:
#: These are product lines ONLY. A CMS source-table token (``pde``,
#: ``inpatient``, ``outpatient``, ``carrier``) is not a product line: it is the
#: separate ``cms_source_table`` field (:data:`CMS_SOURCE_TABLES`), so it is
#: not part of this vocabulary and a table token on ``claim_type`` scores 0.
INSURANCE_PRODUCT_LINES: tuple[str, ...] = (
    "auto",
    "property",
    "liability",
    "health",
    "life",
    "workers_comp",
    "other",
)

#: Closed ``coverage_determination`` vocabulary (0.28.0 ``label``), the outcomes the
#: insurance specialist is told to quote (llm-mailroom ``insurance_claims_specialist``).
COVERAGE_DETERMINATIONS: tuple[str, ...] = ("approved", "denied", "partial", "pending")

#: Closed correspondence ``urgency`` vocabulary (0.28.0 ``label``); never absent:
#: a neutral message is ``routine``.
URGENCY_LEVELS: tuple[str, ...] = ("routine", "time-sensitive", "urgent", "critical")

#: Subclass keys the sorter may emit for an insurance row, in catalog order:
#: the CMS source tables (the 600 CMS rows' ``expected_subclass``) then the
#: product lines. Kept as the historic public name; use
#: :data:`INSURANCE_PRODUCT_LINES` for the scored ``claim_type`` vocabulary.
INSURANCE_CLAIM_TYPES: tuple[str, ...] = (
    "pde",
    "inpatient",
    "outpatient",
    "carrier",
) + INSURANCE_PRODUCT_LINES


#: Explicit CMS DE-SynPUF mapping (dojo #93): source-table token -> the Hub's
#: ``claim_type`` (product line), the subclass and the intent of the row.
#: ``claim_type`` and the source table are **orthogonal**: the product line of
#: every CMS row is ``health``; the table is scored as its own field,
#: ``cms_source_table`` (type ``label``), derived for CMS rows only from
#: ``expected_subclass`` (see :func:`cms_source_table`,
#: ``gt_metadata.scoring_gt_fields`` and ``field_scoring.score_extraction``).
#: The published ``gt_fields`` never carry the key: 600 CMS rows (150 per
#: table; ``source_corpus == "cms_desynpuf"``) of 1,100 insurance rows.
CMS_SOURCE_TABLES: dict[str, dict[str, str]] = {
    table: {"claim_type": "health", "subclass": table, "intent": "claim_data_record"}
    for table in ("carrier", "inpatient", "outpatient", "pde")
}

#: CMS fields the schema documents as absent from DE-SynPUF (GT presence
#: ``schema_documented_absence`` on all 600 rows). On a CMS row an empty
#: prediction is correct and a stated value is a false positive.
CMS_DOCUMENTED_ABSENT_FIELDS: tuple[str, ...] = ("adjuster", "denial_reasons")


def cms_source_table(doc_type: str | None, subclass: Any) -> str | None:
    """The CMS source table of an insurance row, derived from its subclass (else ``None``)."""
    if doc_type != "insurance_claim":
        return None
    token = str(subclass or "").strip().lower()
    return token if token in CMS_SOURCE_TABLES else None


def normalize_corpus_subclass(doc_type: str | None, value: Any) -> str:
    """Normalize a raw subclass into the canonical key for ``doc_type``.

    - ``contract`` → CUAD family via :func:`normalize_subtype`
    - ``merger_agreement`` → MAUD consideration via :func:`normalize_doc_subclass`
    - every other catalogued type → scoped :func:`normalize_doc_subclass`
    - unknown doc type / empty catalog → ``other``
    """
    if not doc_type:
        return SUBTYPE_UNKNOWN
    allowed = DOC_TYPE_SUBCLASSES.get(doc_type)
    if allowed is None:
        return SUBTYPE_UNKNOWN
    if doc_type == "contract":
        return normalize_subtype(value)
    if not allowed:
        return SUBTYPE_UNKNOWN
    return normalize_doc_subclass(value, allowed=set(allowed))


def subclass_equivalent(doc_type: str | None, a: Any, b: Any) -> bool:
    """Family-level equivalence scoped to the doc type's subclass dimension."""
    na = normalize_corpus_subclass(doc_type, a)
    nb = normalize_corpus_subclass(doc_type, b)
    if na == nb:
        return True
    if doc_type == "contract":
        return equivalent_subtypes(na, nb)
    if doc_type == "merger_agreement":
        return equivalent_doc_subclasses(
            na, nb, allowed=set(DOC_TYPE_SUBCLASSES["merger_agreement"])
        )
    return False


def suite_schema(doc_type: str) -> dict[str, Any]:
    """Structured alignment record for one document class."""
    present = doc_type in CORPUS_DOC_TYPES
    return {
        "doc_type": doc_type,
        "in_corpus": present,
        "subclasses": list(DOC_TYPE_SUBCLASSES.get(doc_type, ())),
        "corpus_subclass_surfaces": list(CORPUS_SUBCLASS_SURFACES.get(doc_type, ())),
        "extraction_fields": list(CORPUS_EXTRACTION_FIELDS.get(doc_type, ())),
        "differentiators": list(CORPUS_DIFFERENTIATORS.get(doc_type, ())),
        "honest_gap": (
            None
            if present
            else (
                f"HONEST GAP: {doc_type} is a native mailroom class but has "
                f"zero rows in {CORPUS_ID}; suite scores the extraction schema "
                "only (no corpus-backed subclass dimension)."
            )
        ),
    }
