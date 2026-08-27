"""Hub subclass inventories for every mailroom specialist.

Mirrors ``cuad_maud.py`` for the non-contract classes so corporate records,
correspondence, compliance filings, and insurance claims sort / parse /
extract the same Hub tokens that ``Lucius-Morningstar/docclass-merged``
stores in ``ground_truth.expected_subclass``.

Canonical tokens (Hub ``expected_subclass``):

- ``corporate_record`` — ``articles_of_incorporation``, ``bylaws``,
  ``powers_of_attorney``, ``rights_instrument``, ``other``
- ``correspondence`` — ``email``, ``letter``, ``memo``, ``notice``,
  ``demand``, ``attorney_demand``, ``press_release``, ``meeting_request``
- ``insurance_claim`` — CMS/DE-SynPUF file types ``pde``, ``inpatient``,
  ``outpatient``, ``carrier`` (plus the legacy FNOL lines
  ``auto``/``property``/``liability``/``health``/``life``/``workers_comp``)
- ``compliance_filing`` — SEC form-body types (``10-K``, ``8-K``, …)

Contract / merger inventories stay in ``cuad_maud.py``; this module
dispatches to them so graph handoff / enrich / Boss-skip share one entry.
"""

from __future__ import annotations

import json
import re
from typing import Any


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

COMPLIANCE_FILING_TYPES: tuple[str, ...] = (
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
)

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
)

# Schema fields joined from Hub ground_truth when present. Corporate Hub rows
# today only carry expected_subclass; extra columns are joined, never invented.
CORPORATE_GT_KEYS: tuple[str, ...] = (
    "entity_name",
    "record_type",
    "effective_date",
    "key_provisions",
    "signatories",
    "jurisdiction",
    "filing_number",
)

COMPLIANCE_GT_KEYS: tuple[str, ...] = (
    "filing_type",
    "regulatory_body",
    "filing_date",
    "due_date",
    "entity_name",
    "key_requirements",
    "status",
    "reference_number",
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
    "key_points",
    "demand_amount",
    "action_items",
    "urgency",
    "referenced_communications",
)

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
        "bylaws", "articles_of_incorporation", "certificate_of_formation",
        "charter_amendment", "powers_of_attorney", "subsidiary_list",
        "rights_instrument", "indenture", "board_resolution",
        "officer_certificate", "other",
    ),
    "correspondence": CORRESPONDENCE_TYPES,
    "insurance_claim": ("carrier", "inpatient", "outpatient", "pde"),
    "compliance_filing": COMPLIANCE_FILING_TYPES,
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
            return tuple(tokens)
    except Exception:
        pass
    return _DOJO_SORTER_SUBCLASSES.get(kind, ())


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
    """Canonical subclass for ``doc_type``, or None when empty/uncatalogued."""
    kind = str(doc_type or "")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    catalog = valid_sorter_subclasses(kind)
    if not catalog:
        return None
    if kind == "contract":
        from langchain_agents.sorter_agent import normalize_subtype

        token = normalize_subtype(text)
        return token if token in catalog else None
    try:
        from llm_dojo_scoring.corpus import normalize_corpus_subclass

        token = normalize_corpus_subclass(kind, text)
    except Exception:
        token = None
        compact = re.sub(r"[^a-z0-9]", "", text.lower())
        for key in catalog:
            if re.sub(r"[^a-z0-9]", "", key.lower()) == compact:
                token = key
                break
        if token is None and kind == "insurance_claim":
            token = normalize_claim_type(text) or None
        elif token is None and kind == "corporate_record":
            token = normalize_record_type(text) or None
        elif token is None and kind == "correspondence":
            token = normalize_communication_type(text) or None
        elif token is None and kind == "compliance_filing":
            token = normalize_filing_type(text) or None
    if token in catalog:
        return token
    if text in catalog:
        return text
    return None


def format_sorter_subclass_catalogs() -> str:
    """User-message catalog block (not Mustache — doctrine stays placeholder-free)."""
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
        "compliance_filing",
    )
    notes = {
        "contract": " — also copy this key into contract_subtype; use other if none fit",
        "merger_agreement": " — MAUD consideration type; contract_subtype stays null",
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
    "filing_type",
    "claim_type",
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

_COMPLIANCE_ALIASES = {
    "10k": "10-K",
    "10kanual": "10-K",
    "annualreport": "10-K",
    "10q": "10-Q",
    "quarterlyreport": "10-Q",
    "8k": "8-K",
    "currentreport": "8-K",
    "s1": "S-1",
    "form s1": "S-1",
    "def14a": "DEF 14A",
    "proxy": "DEF 14A",
    "proxy statement": "DEF 14A",
    "13d": "13D",
    "schedule13d": "13D",
    "13g": "13G",
    "schedule13g": "13G",
    "form4": "Form 4",
    "20f": "20-F",
    "6k": "6-K",
}


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _normalize(value: Any, keys: tuple[str, ...], aliases: dict[str, str]) -> str:
    """Map a free-text label onto a canonical inventory token.

    Longest canonical key wins so ``attorney_demand`` is not swallowed by
    ``demand``, and ``articles_of_incorporation`` is not swallowed by a
    short alias collision.
    """
    key = _compact(value)
    if not key:
        return ""
    compact_keys = {_compact(k): k for k in keys}
    if key in compact_keys:
        return compact_keys[key]
    alias_norm = {_compact(k): v for k, v in aliases.items()}
    if key in alias_norm:
        return alias_norm[key]
    ranked_aliases = sorted(aliases.items(), key=lambda kv: -len(_compact(kv[0])))
    for alias, canonical in ranked_aliases:
        ak = _compact(alias)
        if len(ak) >= 8 and (key.startswith(ak) or ak in key):
            return canonical
    ranked = sorted(keys, key=lambda item: -len(_compact(item)))
    for canonical in ranked:
        ck = _compact(canonical)
        if not ck or canonical == "other":
            continue
        if key.startswith(ck):
            return canonical
    for canonical in ranked:
        ck = _compact(canonical)
        if not ck or canonical == "other":
            continue
        if len(ck) >= 4 and ck in key:
            return canonical
        if len(ck) >= 3 and ck[:1].isdigit() and ck in key:
            return canonical
    if key == "other":
        return "other"
    return ""


def normalize_record_type(value: Any) -> str:
    return _normalize(value, CORPORATE_RECORD_TYPES, _CORPORATE_ALIASES)


def normalize_communication_type(value: Any) -> str:
    return _normalize(value, CORRESPONDENCE_TYPES, _CORRESPONDENCE_ALIASES)


def normalize_claim_type(value: Any) -> str:
    return _normalize(value, INSURANCE_CLAIM_TYPES, _INSURANCE_ALIASES)


def normalize_filing_type(value: Any) -> str:
    return _normalize(value, COMPLIANCE_FILING_TYPES, _COMPLIANCE_ALIASES)


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
    "Enron-style inbox messages are email. Internal memoranda are memo. "
    "Calendar or meeting invites are meeting_request. News/press releases "
    "are press_release. Formal demand letters are demand; attorney-signed "
    "demands are attorney_demand."
)

CLAIM_TYPE_DESCRIPTION = (
    "Canonical claim-file / line token. For CMS/DE-SynPUF claim tables emit "
    "exactly one of: pde (Part D Event / prescription), inpatient, "
    "outpatient, carrier (professional/physician/carrier). For traditional "
    "FNOL/policy documents emit: auto, property, liability, health, life, "
    "workers_comp. Use other only when none of those fit. Never leave this "
    "empty when table headers identify a CMS file type."
)

FILING_TYPE_DESCRIPTION = (
    "SEC form type of THIS document's body: 10-K, 10-Q, 8-K, S-1, DEF 14A, "
    "13D, 13G, Form 4, 20-F, 6-K, or other. If this file is only an exhibit "
    "(articles, bylaws, rights instrument, specimen stock), that is a "
    "corporate_record — do not treat the wrapping form name as filing_type."
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
            "An S-1/10-K exhibit wrapper does not make this a compliance filing."
        )
    if kind == "correspondence":
        return (
            "CORRESPONDENCE INVENTORY — set communication_type to exactly one of: "
            + ", ".join(CORRESPONDENCE_TYPES)
            + ". Readable emails/memos/meeting requests are never unknown; "
            "Enron-style inbox text is email."
        )
    if kind == "insurance_claim":
        return (
            "INSURANCE CLAIM INVENTORY — set claim_type to a Hub token. "
            "CMS/DE-SynPUF tables: "
            + ", ".join(INSURANCE_CLAIM_TYPES[:4])
            + ". Traditional FNOL lines: "
            + ", ".join(INSURANCE_CLAIM_TYPES[4:])
            + ". PDE/CLM_ID/DESYNPUF headers identify the CMS file type; "
            "do not classify those tables as compliance_filing."
        )
    if kind == "compliance_filing":
        return (
            "COMPLIANCE FILING INVENTORY — set filing_type to the form BODY: "
            + ", ".join(COMPLIANCE_FILING_TYPES)
            + ". Attached charters, bylaws, POA, and rights instruments are "
            "corporate_record, not this class."
        )
    return ""


def enrich_extraction(
    extracted: dict | None,
    *,
    doc_type: str | None = None,
    extract_class: str | None = None,
    subtype: str | None = None,
) -> dict:
    """Fill Hub inventory fields without overwriting a specialist value."""
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
        token = normalize_communication_type(
            result.get("communication_type") or subtype
        )
        if token:
            result["communication_type"] = token
    elif kind == "insurance_claim":
        token = normalize_claim_type(result.get("claim_type") or subtype)
        if token:
            result["claim_type"] = token
    elif kind == "compliance_filing":
        token = normalize_filing_type(result.get("filing_type") or subtype)
        if token:
            result["filing_type"] = token
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
