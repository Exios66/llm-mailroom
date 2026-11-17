"""Hub subclass inventories for every mailroom specialist.

Mirrors ``cuad_maud.py`` for the non-contract classes so corporate records,
correspondence, and insurance claims sort / parse /
extract the same Hub tokens that ``Lucius-Morningstar/mailroom-dataset``
stores in ``ground_truth.expected_subclass``.

Canonical tokens (Hub ``expected_subclass``):

- ``corporate_record`` — ``articles_of_incorporation``, ``bylaws``,
  ``powers_of_attorney``, ``rights_instrument``, ``other``
- ``correspondence`` — ``email``, ``letter``, ``memo``, ``notice``,
  ``demand``, ``attorney_demand``, ``press_release``, ``meeting_request``
- ``insurance_claim`` — CMS/DE-SynPUF file types ``pde``, ``inpatient``,
  ``outpatient``, ``carrier`` (plus the legacy FNOL lines
  ``auto``/``property``/``liability``/``health``/``life``/``workers_comp``).
  These are sorter subclasses; the extracted ``claim_type`` is the product line
  only (``INSURANCE_PRODUCT_LINES``), and a CMS table's line is ``health`` (#174 §2).

Contract / merger inventories stay in ``cuad_maud.py``; this module
dispatches to them so graph handoff / enrich / Boss-skip share one entry.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Intent vocabulary, descriptions, aliases and normalizer live in the dojo
# (single source of truth); re-exported so callers keep importing from here.
from llm_dojo_scoring.intents import (  # noqa: F401
    INTENT_ALIASES as _INTENT_ALIASES,
)
from llm_dojo_scoring.intents import (  # noqa: F401
    INTENT_DESCRIPTIONS,
    INTENT_LABELS,
    normalize_intent,
)

CORPORATE_RECORD_TYPES: tuple[str, ...] = (
    "articles_of_incorporation",
    "bylaws",
    "powers_of_attorney",
    "rights_instrument",
    "other",
)

CORRESPONDENCE_TYPES: tuple[str, ...] = (
    "email",
    "letter",
    "memo",
    "notice",
    "demand",
    "attorney_demand",
    "press_release",
    "meeting_request",
)

# Hub CMS/DE-SynPUF tokens first; legacy FNOL lines remain valid.
INSURANCE_CLAIM_TYPES: tuple[str, ...] = (
    "pde",
    "inpatient",
    "outpatient",
    "carrier",
    "auto",
    "property",
    "liability",
    "health",
    "life",
    "workers_comp",
    "other",
)

#: The CMS/DE-SynPUF source-table tokens: sorter subclasses, never a ``claim_type``.
CMS_CLAIM_TABLES: tuple[str, ...] = INSURANCE_CLAIM_TYPES[:4]
#: The scored ``claim_type`` vocabulary (Dojo ``INSURANCE_PRODUCT_LINES``, #174 §2). Since
#: llm-dojo-scoring#93 the CMS table is its own field and a table token on ``claim_type``
#: scores 0; every CMS row's product line is ``health``.
INSURANCE_PRODUCT_LINES: tuple[str, ...] = INSURANCE_CLAIM_TYPES[4:]

INSURANCE_GT_KEYS: tuple[str, ...] = (
    "claim_number",
    "policy_number",
    "insurer",
    "insured_party",
    "claim_type",
    "date_of_loss",
    "date_filed",
    "claimed_amount",
    "adjuster",
    "damages_description",
    "coverage_determination",
    "denial_reasons",
    "supporting_documents",
    "intent",
    "subject_matter",
    "keywords",
    "claim_checklist",
)

# Schema fields joined from Hub ground_truth when present. Corporate Hub rows
# today only carry expected_subclass; extra columns are joined, never invented.
CORPORATE_GT_KEYS: tuple[str, ...] = (
    "entity_name",
    "record_type",
    "effective_date",
    "intent",
    "subject_matter",
    "keywords",
    "signatories",
    "jurisdiction",
    "filing_number",
)

# Correspondence schema fields joined from Hub extra columns when present.
# Enron rows typically only carry subclass + topic/sentiment extras; the
# rest is filled post-hoc from headers (From/To/Date) in extraction_gt.
CORRESPONDENCE_GT_KEYS: tuple[str, ...] = (
    "sender",
    "recipient",
    "additional_recipients",
    "communication_type",
    "communication_date",
    "intent",
    "subject_matter",
    "keywords",
    "demand_amount",
    "action_items",
    "urgency",
    "referenced_communications",
)

# === Intent (purpose) controlled vocabulary ================================
# The vocabulary, descriptions, aliases and ``normalize_intent`` live in
# llm-dojo-scoring (``llm_dojo_scoring.intents``, the single source of truth;
# the dojo ``label`` field type scores against the same closed sets). They are
# re-exported here so existing callers keep importing from this module.
# ``_INTENT_ALIASES`` is kept as an alias of the dojo ``INTENT_ALIASES``.


# Sorter subclass catalogs from llm-dojo-scoring 0.9.0 (PR #4). Hub extraction
# inventories above stay narrower (corporate_record is five tokens; insurance
# extract still accepts FNOL lines). Do not replace CORPORATE_RECORD_TYPES.
_DOJO_SORTER_SUBCLASSES: dict[str, tuple[str, ...]] = {
    "contract": (
        "affiliate", "agency", "collaboration", "co_branding", "consulting",
        "development", "distributor", "endorsement", "franchise", "hosting",
        "ip", "joint_venture", "license", "maintenance", "manufacturing",
        "marketing", "non_compete_no_solicit", "outsourcing", "promotion",
        "reseller", "service", "sponsorship", "strategic_alliance", "supply",
        "transportation",
    ),
    "merger_agreement": (
        "all_cash", "all_stock", "mixed_cash_stock",
        "mixed_cash_stock_election", "other",
    ),
    "corporate_record": (
        "articles_of_incorporation", "bylaws", "certificate_of_formation",
        "charter_amendment", "board_resolution", "officer_certificate",
        "powers_of_attorney", "rights_instrument", "indenture",
        "subsidiary_list", "other",
    ),
    "correspondence": CORRESPONDENCE_TYPES,
    "insurance_claim": ("carrier", "inpatient", "outpatient", "pde", "property", "auto"),
    "due_diligence": (),
    "court_opinion": (),
}


def sorter_subclass_catalog(doc_type: str | None) -> tuple[str, ...]:
    """Dojo per-class sorter catalog (empty for unknown / retired types)."""
    kind = str(doc_type or "")
    try:
        from llm_dojo_scoring.corpus import DOC_TYPE_SUBCLASSES

        tokens = DOC_TYPE_SUBCLASSES.get(kind)
        if tokens is not None:
            return _reorder_corporate_subclasses(kind, tuple(tokens))
    except ImportError:
        pass
    tokens = _DOJO_SORTER_SUBCLASSES.get(kind, ())
    return _reorder_corporate_subclasses(kind, tokens)


def _reorder_corporate_subclasses(
    doc_type: str, tokens: tuple[str, ...]
) -> tuple[str, ...]:
    if doc_type != "corporate_record" or "bylaws" not in tokens:
        return tokens
    lst = list(tokens)
    lst.remove("bylaws")
    idx = 1 if "articles_of_incorporation" in lst else 0
    lst.insert(idx, "bylaws")
    return tuple(lst)


def valid_sorter_subclasses(doc_type: str | None) -> frozenset[str]:
    """Catalog keys the classification guard accepts for ``doc_subclass``.

    Contract adds CUAD ``other``. Insurance also accepts Hub FNOL lines
    (extract ``claim_type``); the sorter prompt lists CMS tokens first.
    """
    kind = str(doc_type or "")
    keys = set(sorter_subclass_catalog(kind))
    if kind == "contract":
        keys.add("other")
    elif kind == "insurance_claim":
        keys.update(INSURANCE_CLAIM_TYPES)
    return frozenset(keys)


def normalize_sorter_subclass(doc_type: str | None, value: Any) -> str | None:
    """Canonical scoped normalization over the producer's accepted tokens."""
    from llm_dojo_scoring.pilot_labels import normalize_sorter_token

    kind = str(doc_type or "")
    token = None
    if kind == "contract":
        from langchain_agents.sorter_agent import normalize_subtype

        token = normalize_subtype(value)
    return normalize_sorter_token(kind, value, valid_sorter_subclasses(kind), contract_token=token)


def format_sorter_subclass_catalogs() -> str:
    """User-message catalog block (not Mustache — doctrine stays placeholder-free).

    Renders the dojo sorter guide (``llm_dojo_scoring.sorter_guide``): per class
    the keys a sorter may emit and the corpus meaning of each. A bare key list
    made sorter V14 call CMS carrier tables outpatient, Certificates of
    Designation rights_instrument and charter amendments certificate_of_formation,
    and ignore the correspondence marker precedence. Falls back to the key list
    on a dojo without the guide.
    """
    try:
        from llm_dojo_scoring.sorter_guide import format_sorter_subclass_guide
    except ImportError:
        return _format_bare_subclass_catalogs()
    lines = [
        "DOCUMENT SUBCLASS CATALOGS — when the chosen doc_type has a catalog, "
        "emit doc_subclass as exactly one of that class's keys (the text after "
        "each key is what the label means in this corpus). "
        "contract_subtype is CUAD-only (required for contract, null otherwise). "
        "content_topic and sentiment_label are not sorter outputs.",
    ]
    for key in _SORTER_CATALOG_ORDER:
        block = format_sorter_subclass_guide(key)
        if block:
            lines.append(block)
    return "\n".join(lines)


_SORTER_CATALOG_ORDER = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)


def _format_bare_subclass_catalogs() -> str:
    """Pre-guide catalog: comma-separated keys per class (dojo < sorter_guide)."""
    lines = [
        "DOCUMENT SUBCLASS CATALOGS — when the chosen doc_type has a catalog, "
        "emit doc_subclass as exactly one of that class's keys. "
        "contract_subtype is CUAD-only (required for contract, null otherwise). "
        "content_topic and sentiment_label are not sorter outputs.",
    ]
    order = (
        "contract",
        "merger_agreement",
        "corporate_record",
        "correspondence",
        "insurance_claim",
    )
    notes = {
        "contract": " — also copy this key into contract_subtype; use other if none fit",
        "merger_agreement": " — MAUD consideration type; contract_subtype stays null",
        "corporate_record": (
            " — articles_of_incorporation (charters/formation docs), "
            "bylaws (operating rules/procedures), "
            "rights_instrument (stock/warrant/rights agreements), "
            "powers_of_attorney (delegation/authorization); use other if none fit"
        ),
        "insurance_claim": (
            " — CMS file types; FNOL/policy lines "
            "auto/property/liability/health/life/workers_comp are also valid"
        ),
    }
    for key in order:
        tokens = sorter_subclass_catalog(key)
        if not tokens:
            continue
        extra = notes.get(key, "")
        lines.append(f"- {key}: {', '.join(tokens)}{extra}")
    return "\n".join(lines)

_INVENTORY_FIELDS = {
    "record_type",
    "communication_type",
    "claim_type",
    "cms_source_table",
}

_CORPORATE_ALIASES = {
    "articlesofincorporation": "articles_of_incorporation",
    "articleofincorporation": "articles_of_incorporation",
    "certificateofincorporation": "articles_of_incorporation",
    "certificatesofincorporation": "articles_of_incorporation",
    "certificateofformation": "articles_of_incorporation",
    "articlesofformation": "articles_of_incorporation",
    "charter": "articles_of_incorporation",
    "corporatecharter": "articles_of_incorporation",
    "bylaw": "bylaws",
    "bylaws": "bylaws",
    "byelaws": "bylaws",
    "powerofattorney": "powers_of_attorney",
    "powersofattorney": "powers_of_attorney",
    "poa": "powers_of_attorney",
    "rightsinstrument": "rights_instrument",
    "rightsagreement": "rights_instrument",
    "stockholderrights": "rights_instrument",
    "stockholderights": "rights_instrument",
    "warrant": "rights_instrument",
    "warrants": "rights_instrument",
    "preferredstock": "rights_instrument",
    "specimenstock": "rights_instrument",
    "specimencertificate": "rights_instrument",
    "stockcertificate": "rights_instrument",
}

_CORRESPONDENCE_ALIASES = {
    "email": "email",
    "e-mail": "email",
    "inbox": "email",
    "message": "email",
    "letter": "letter",
    "memo": "memo",
    "memorandum": "memo",
    "notice": "notice",
    "demand": "demand",
    "demandletter": "demand",
    "attorneydemand": "attorney_demand",
    "attorneydemandletter": "attorney_demand",
    "lawyerdemand": "attorney_demand",
    "lawyerdemandletter": "attorney_demand",
    "attorneyletter": "attorney_demand",
    "pressrelease": "press_release",
    "newswire": "press_release",
    "meetingrequest": "meeting_request",
    "meetinginvite": "meeting_request",
    "calendarinvite": "meeting_request",
    "invitation": "meeting_request",
}

_INSURANCE_ALIASES = {
    "pde": "pde",
    "partd": "pde",
    "partdevent": "pde",
    "prescription": "pde",
    "inpatient": "inpatient",
    "hospitalinpatient": "inpatient",
    "outpatient": "outpatient",
    "hospitaloutpatient": "outpatient",
    "carrier": "carrier",
    "professional": "carrier",
    "physician": "carrier",
    "auto": "auto",
    "automobile": "auto",
    "property": "property",
    "homeowners": "property",
    "liability": "liability",
    "health": "health",
    "life": "life",
    "workerscomp": "workers_comp",
    "workerscompensation": "workers_comp",
    "workcomp": "workers_comp",
}


def _compact(value: Any) -> str:
    """Compatibility alias for the canonical compact-label policy."""
    from llm_dojo_scoring.pilot_labels import compact_label

    return compact_label(value)


def _normalize(value: Any, keys: tuple[str, ...], aliases: dict[str, str]) -> str:
    """Canonical inventory matching over the unchanged producer catalog."""
    from llm_dojo_scoring.pilot_labels import normalize_inventory_label

    return normalize_inventory_label(value, keys, aliases)


def normalize_record_type(value: Any) -> str:
    return _normalize(value, CORPORATE_RECORD_TYPES, _CORPORATE_ALIASES)


def normalize_communication_type(value: Any) -> str:
    return _normalize(value, CORRESPONDENCE_TYPES, _CORRESPONDENCE_ALIASES)


def normalize_claim_type(value: Any) -> str:
    return _normalize(value, INSURANCE_CLAIM_TYPES, _INSURANCE_ALIASES)


def normalize_claim_line(value: Any) -> str:
    """``claim_type`` as a product line: a CMS table token (or a table subclass) is ``health``."""
    token = normalize_claim_type(value)
    return "health" if token in CMS_CLAIM_TABLES else token


def cms_source_table_for(subtype: Any, claim_type: Any = None) -> str | None:
    """The CMS/DE-SynPUF source table of an insurance row, derived, never asked of the model.

    Dojo scores ``cms_source_table`` as its own label (llm-dojo-scoring#93), derived from
    ``expected_subclass``. The pipeline derives it the same way from the sorter subclass.
    Only when no subclass is known does a table token the specialist wrote on
    ``claim_type`` (the frozen v1 prompt still asks for one) stand in, read before that
    token is folded onto ``health``. A row whose product line is not ``health`` is not a
    CMS row, whatever the subclass says, so it carries no table.
    """
    sub = normalize_claim_type(subtype) if subtype else ""
    own = normalize_claim_type(claim_type) if claim_type else ""
    if sub and sub != "other":
        table = sub if sub in CMS_CLAIM_TABLES else None
    else:
        table = own if own in CMS_CLAIM_TABLES else None
    if table and own and own not in CMS_CLAIM_TABLES and own != "health":
        return None
    return table


RECORD_TYPE_DESCRIPTION = (
    "Canonical Hub subclass. Emit exactly one of: articles_of_incorporation, "
    "bylaws, powers_of_attorney, rights_instrument, other. "
    "articles_of_incorporation = Certificate/Articles of Incorporation or "
    "Formation. bylaws = corporate bylaws. powers_of_attorney = POA. "
    "rights_instrument = stockholder rights, warrants, preferred-stock "
    "certificates, specimen stock. other = residual. Never emit an SEC form "
    "type (S-1, 10-K, 8-K) as record_type — an exhibit cover sheet does not "
    "change the record."
)

COMMUNICATION_TYPE_DESCRIPTION = (
    "Canonical Hub subclass. Emit exactly one of: email, letter, memo, "
    "notice, demand, attorney_demand, press_release, meeting_request. "
    "Type the content, not the carrier: a memo, letter, notice, demand, "
    "press release or meeting request sent, forwarded or pasted in an email "
    "takes that type. email only for ordinary email correspondence with no "
    "other form inside it. Internal memoranda are memo. Calendar or meeting "
    "invites are meeting_request. News/press releases are press_release. "
    "Formal demand letters are demand; attorney-signed demands are "
    "attorney_demand."
)

SUPPORTING_DOCUMENTS_DESCRIPTION = (
    "Referenced supporting documents (police report, receipts, medical records, "
    "estimates). On a CMS/DE-SynPUF claim row, list the identifier references "
    "instead, one item each, in these exact forms: 'facility provider <id>', "
    "'provider NPI <10 digits>', 'HCPCS detail (<n> codes)', and for a Part D "
    "event the drug product as 'NDC <11 digits>'."
)

CLAIM_TYPE_DESCRIPTION = (
    "Line of business. Emit exactly one of: auto, property, liability, "
    "health, life, workers_comp, other. A CMS/DE-SynPUF claim table "
    "(Part D event, inpatient, outpatient or carrier/physician claim) is "
    "health; never put a table name (pde, inpatient, outpatient, carrier) "
    "here. Use other only when none of those fit."
)


def specialist_handoff(doc_type: str | None, subtype: str | None = None) -> str:
    """Additive extract-node instructions listing the Hub inventory."""
    kind = str(doc_type or "")
    if kind in ("contract", "merger_agreement"):
        from langchain_agents.cuad_maud import clause_handoff

        return clause_handoff(kind, subtype)
    if kind == "corporate_record":
        return (
            "CORPORATE RECORD INVENTORY — set record_type to exactly one of: "
            + ", ".join(CORPORATE_RECORD_TYPES)
            + ". articles_of_incorporation covers Certificate/Articles of "
            "Incorporation or Formation; rights_instrument covers stockholder "
            "rights, warrants, preferred certificates, and specimen stock. "
            "An S-1/10-K exhibit wrapper does not change the record type."
        )
    if kind == "correspondence":
        return (
            "CORRESPONDENCE INVENTORY — set communication_type to exactly one of: "
            + ", ".join(CORRESPONDENCE_TYPES)
            + ". Readable emails/memos/meeting requests are never unknown. "
            "Type the content, not the carrier: a memo, letter, notice, demand, "
            "press release or meeting request sent or forwarded in an email "
            "takes that type; email only for ordinary email correspondence."
        )
    if kind == "insurance_claim":
        return (
            "INSURANCE CLAIM INVENTORY — set claim_type to the line of business, "
            "exactly one of: "
            + ", ".join(INSURANCE_PRODUCT_LINES)
            + ". CMS/DE-SynPUF tables (PDE/CLM_ID/DESYNPUF headers: "
            + ", ".join(CMS_CLAIM_TABLES)
            + ") are insurance_claim claims whose claim_type is health; "
            "the table name is never a claim_type (the pipeline records it as "
            "cms_source_table from the subclass). On those rows supporting_documents "
            "holds the identifier references: facility provider <id>, provider NPI "
            "<10 digits>, HCPCS detail (<n> codes), and on a Part D event the drug "
            "product as NDC <11 digits>."
        )
    return ""


def enrich_extraction(
    extracted: dict | None,
    *,
    doc_type: str | None = None,
    extract_class: str | None = None,
    subtype: str | None = None,
) -> dict:
    """Fill Hub inventory fields without overwriting a specialist value.

    Inventory/type tokens are canonicalized; a free-text ``intent`` is mapped
    onto the class's controlled vocabulary (``normalize_intent``) when it maps,
    and left untouched when it does not — the extract path therefore emits the
    canonical label rather than whatever the model chose (HUB intent fix).
    """
    kind = str(doc_type or "")
    resolved = str(extract_class or kind)
    if resolved == "contract" or kind in ("contract", "merger_agreement"):
        from langchain_agents.cuad_maud import enrich_contract_extraction

        return enrich_contract_extraction(
            extracted,
            doc_type=kind,
            contract_subtype=subtype,
        )
    result = dict(extracted or {})
    if kind == "corporate_record":
        token = normalize_record_type(result.get("record_type") or subtype)
        if token:
            result["record_type"] = token
    elif kind == "correspondence":
        own = result.get("communication_type")
        token = normalize_communication_type(own or subtype)
        # The model's own null is a valid contract answer; do not backfill
        # it from a sorter subtype that only normalizes to the residual "other".
        if token and not (not own and token == "other"):
            result["communication_type"] = token
    elif kind == "insurance_claim":
        own = result.get("claim_type")
        token = normalize_claim_line(own or subtype)
        if token:
            result["claim_type"] = token
        # Read the table before the fold above discards a table token on claim_type.
        table = cms_source_table_for(subtype, own or token)
        if table:
            result["cms_source_table"] = table
        else:
            result.pop("cms_source_table", None)
    if kind in INTENT_LABELS:
        intent = normalize_intent(kind, result.get("intent"))
        if intent:
            result["intent"] = intent
    return result


def skip_conflict_field(name: str) -> bool:
    """Boss same-class skip: CUAD/MAUD inventories plus Hub type tokens."""
    from langchain_agents.cuad_maud import skip_conflict_field as cuad_skip

    return cuad_skip(name) or name in _INVENTORY_FIELDS


def coerce_gt_value(value: Any) -> Any:
    """Parse Hub ground-truth cells that arrive as JSON strings."""
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value
