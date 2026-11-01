"""Corporate-record entity schema — one contract per record subclass.

``corporate_record`` is not one document shape. A power of attorney's key
entities are its principal and attorney-in-fact; an indenture's are its issuer,
trustee and guarantors; a subsidiary list's are the subsidiaries themselves.
The flat nine-field ``CorporateRecordExtraction`` (``entity_name`` /
``record_type`` / ``effective_date`` / ``signatories`` / ``jurisdiction`` /
``filing_number`` / ``intent`` / ``subject_matter`` / ``keywords``) is a shared
*core*; this module makes the per-subclass layer data instead of prose:

* :data:`SUBCLASS_SCHEMAS` — for each of the 11 sorter subclasses: the
  ``record_type`` token it maps to, the GT-pinned ``intent``, which core fields
  apply, and the subclass's own **key entities** (:class:`RecordEntity`).
* :func:`scored_field_types` — the field→scoring-type map for one subclass.
* :func:`render_subclass_block` — the prompt text for the specialist, generated
  from the same data so prompt and scorer cannot drift.
* :func:`corporate_record_json_schema` — JSON Schema for one subclass.
* :func:`unlabeled_fields` / :func:`align_unlabeled` — the GT-honesty rule: a
  key-entity field that the dataset leaves blank *without* documenting absence
  is **unlabeled**, not "expected empty", and is excluded from both sides of the
  comparison. Without this a correct entity extraction is scored as a spurious
  fill (published v9 GT has no key-entity label on any of the 450 rows).
* :func:`audit_gt_row` / :func:`audit_gt_rows` — label-quality audit.

Core types are asserted equal to ``DEFAULT_FIELD_TYPES["corporate_record"]`` at
import time. Extension entities are scored only once the dataset labels them
(``gt_presence`` ``populated`` / ``schema_documented_absence``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .corpus import DOC_TYPE_SUBCLASSES
from .gt_metadata import (
    gt_presence_map,
    is_empty_value,
    parse_gt_fields,
    parse_json_container,
)
from .suites import DEFAULT_FIELD_TYPES

__all__ = [
    "CORE_FIELDS",
    "ENTITY_CORE_FIELDS",
    "RECORD_TYPES",
    "SUBCLASS_SCHEMAS",
    "RecordEntity",
    "SubclassSchema",
    "align_unlabeled",
    "all_entity_field_types",
    "audit_gt_row",
    "audit_gt_rows",
    "corporate_record_json_schema",
    "entity_field_types",
    "record_type_for_subclass",
    "render_prompt_section",
    "render_subclass_block",
    "scored_field_types",
    "subclass_schema",
    "unlabeled_fields",
]

CORE_FIELDS: tuple[str, ...] = tuple(DEFAULT_FIELD_TYPES["corporate_record"])

#: Core fields that name a real-world entity / fact in the document. These are
#: the fields the published GT leaves blank; ``intent`` / ``subject_matter`` /
#: ``keywords`` are the semantic trio and are labeled for every row.
ENTITY_CORE_FIELDS: tuple[str, ...] = (
    "entity_name",
    "record_type",
    "effective_date",
    "signatories",
    "jurisdiction",
    "filing_number",
)

#: The 5-token extraction enum (Hub ``record_type``).
RECORD_TYPES: tuple[str, ...] = (
    "articles_of_incorporation",
    "bylaws",
    "powers_of_attorney",
    "rights_instrument",
    "other",
)


@dataclass(frozen=True)
class RecordEntity:
    """One subclass-specific key entity (an extension field)."""

    key: str
    type: str  # a scoring type from field_scoring: name / date / money / ...
    description: str
    where: str  # where in the document it lives
    required: bool = False  # present on a well-formed instance of the subclass


@dataclass(frozen=True)
class SubclassSchema:
    """One corporate-record subclass: applicable core fields, entities, and prompt clauses.

    ``omit_entity_keys`` and ``core_guidance`` are the two clauses the field
    list alone cannot express. ``intent_review`` is set when the pinned
    ``intent`` is kept for score comparability but is debatable.
    """

    subclass: str
    label: str
    record_type: str
    intent: str  # intent pinned by the published GT for this subclass
    summary: str
    core_applicable: tuple[str, ...]
    entities: tuple[RecordEntity, ...] = ()
    notes: str = ""
    intent_review: str = ""  # why the pinned intent is debatable, if it is
    #: Extension-entity keys the extraction must NOT emit at all — not even as
    #: null / [] (rendered as the prompt's "; omit A entirely, even when null").
    omit_entity_keys: tuple[str, ...] = ()
    #: Per-core-field semantic guidance rendered into the prompt line
    #: ("; subject_matter = ..., keywords = ...") for mappings the schema
    #: structure alone cannot express (e.g. the subsidiary list schedule).
    core_guidance: tuple[tuple[str, str], ...] = ()

    @property
    def core_not_applicable(self) -> tuple[str, ...]:
        """Core fields outside this subclass, in ``CORE_FIELDS`` order."""
        return tuple(f for f in CORE_FIELDS if f not in self.core_applicable)

    def entity(self, key: str) -> RecordEntity:
        """Return the extension entity with this key; raise ``KeyError`` if absent."""
        for ent in self.entities:
            if ent.key == key:
                return ent
        raise KeyError(f"{self.subclass} has no entity {key!r}")


_BASE = ("entity_name", "record_type", "effective_date", "intent", "subject_matter", "keywords")
_ALL = CORE_FIELDS


def _s(*names: str) -> tuple[str, ...]:
    """Select known core fields from *names* in ``CORE_FIELDS`` order."""
    return tuple(f for f in CORE_FIELDS if f in names)


_E = RecordEntity

SUBCLASS_SCHEMAS: dict[str, SubclassSchema] = {
    s.subclass: s
    for s in (
        SubclassSchema(
            "bylaws", "Bylaws", "bylaws", "governance_rules",
            "Internal operating rules of the corporation (meetings, board, officers, "
            "indemnification, amendment).",
            _s(*_BASE, "jurisdiction", "signatories"),
            (
                _E("certifying_officer", "name", "Secretary/officer who certifies the bylaws as adopted.",
                   "Secretary's certificate or closing attestation"),
                _E("last_amended_date", "date", "Date of the most recent amendment/restatement stated.",
                   "Title block, 'Amended and Restated as of', or adoption legend"),
            ),
            "No filing_number: bylaws are not filed with a state office.",
        ),
        SubclassSchema(
            "articles_of_incorporation", "Articles / Certificate of Incorporation",
            "articles_of_incorporation", "entity_formation",
            "Charter that creates a corporation (name, purpose, share structure, agent).",
            _ALL,
            (
                _E("incorporators", "entity_list:name", "Persons who signed to form the corporation.",
                   "Signature block / 'Incorporator' line"),
                _E("registered_agent", "name", "Registered agent for service of process.",
                   "'Registered Agent' / 'Resident Agent' article", True),
                _E("authorized_capital", "free_text", "Authorized share classes and counts, as written.",
                   "Capital stock article"),
            ),
        ),
        SubclassSchema(
            "certificate_of_formation", "Certificate of Formation (LLC)",
            "articles_of_incorporation", "entity_formation",
            "Filing that forms an LLC or similar non-corporate entity.",
            _ALL,
            (
                _E("organizers", "entity_list:name", "Persons who signed to form the entity.",
                   "Signature block / 'Organizer' line"),
                _E("registered_agent", "name", "Registered agent for service of process.",
                   "'Registered Agent' article", True),
            ),
            "Hub inventory has no rows for this subclass; kept as an alias of "
            "articles_of_incorporation. An LLC certificate has no capital-article share "
            "schedule, so the prompt forbids emitting authorized_capital even as null.",
            omit_entity_keys=("authorized_capital",),
        ),
        SubclassSchema(
            "charter_amendment", "Certificate of Amendment / Restated Charter",
            "articles_of_incorporation", "entity_formation",
            "Filing that amends or restates an existing charter.",
            _ALL,
            (
                _E("amended_provisions", "entity_list:free_text",
                   "Each charter article/section amended and what changes (one item per provision).",
                   "'Article X is hereby amended to read'", True),
                _E("approval_date", "date", "Date the board/stockholders adopted the amendment.",
                   "Adoption recital"),
                _E("prior_entity_name", "name", "Former legal name when the amendment changes the name.",
                   "Name-change article"),
            ),
            intent_review="Pinned entity_formation; an amendment approving a corporate action is "
            "arguably corporate_action_approval.",
        ),
        SubclassSchema(
            "powers_of_attorney", "Power of Attorney", "powers_of_attorney", "authority_delegation",
            "Grant of authority from a principal to an attorney-in-fact (often to sign SEC filings).",
            _s(*_BASE, "signatories", "jurisdiction"),
            (
                _E("principal", "name", "Person or entity granting the authority.",
                   "'The undersigned' / signature block", True),
                _E("attorneys_in_fact", "entity_list:name", "Persons empowered to act.",
                   "'hereby constitutes and appoints'", True),
                _E("authority_scope", "free_text", "What the attorney-in-fact may do.",
                   "Grant clause"),
                _E("expiration_date", "date", "Date authority lapses, when stated.", "Term clause"),
            ),
            "entity_name is the registrant the POA relates to; the principal may be an individual "
            "director/officer.",
        ),
        SubclassSchema(
            "subsidiary_list", "List of Subsidiaries", "other", "other",
            "Schedule of the registrant's subsidiaries (typically Exhibit 21).",
            _s("entity_name", "record_type", "effective_date", "intent", "subject_matter", "keywords"),
            (
                _E("subsidiaries", "entity_list:name", "Each listed subsidiary, legal name as written.",
                   "Subsidiary table, first column", True),
                _E("subsidiary_jurisdictions", "entity_list:name",
                   "Distinct jurisdictions of organization listed for subsidiaries.",
                   "Subsidiary table, jurisdiction column"),
            ),
            "entity_name is the parent/registrant. No signatories, filing_number or governing "
            "jurisdiction: a schedule is not executed or filed on its own.",
            core_guidance=(("subject_matter", "the listing statement"), ("keywords", "subsidiary names")),
        ),
        SubclassSchema(
            "rights_instrument", "Rights / Warrant / Preferred / Specimen-Stock Instrument",
            "rights_instrument", "investor_rights",
            "Instrument that grants securities holders rights (warrants, stockholder rights, "
            "preferred designations, registration rights, specimen certificates).",
            _s(*_BASE, "signatories", "jurisdiction"),
            (
                _E("instrument_kind", "name",
                   "Kind of instrument: warrant / rights plan / preferred stock / registration "
                   "rights / specimen stock.", "Title and first recital", True),
                _E("holder", "name", "Registered holder or beneficiary, when a named party.",
                   "Face of warrant / schedule"),
                _E("rights_agent", "name", "Rights/warrant/transfer agent.", "Agent signature block"),
                _E("securities_covered", "free_text", "Class and number of securities, as written.",
                   "Operative grant"),
                _E("exercise_price", "money", "Exercise/strike price, as written.", "Exercise clause"),
            ),
        ),
        SubclassSchema(
            "indenture", "Indenture / Debt Instrument", "other", "investor_rights",
            "Contract between an issuer and a trustee governing a debt issue.",
            _s(*_BASE, "signatories", "jurisdiction"),
            (
                _E("trustee", "name", "Indenture trustee.", "Preamble, 'as Trustee'", True),
                _E("guarantors", "entity_list:name", "Guarantors of the notes.", "Guarantor signature pages"),
                _E("securities_series", "name", "Designation of the notes, e.g. '5.25% Senior Notes due 2030'.",
                   "Title and Section 2.01", True),
                _E("principal_amount", "money", "Aggregate principal amount, as written.",
                   "Terms of the securities"),
                _E("maturity_date", "date", "Stated maturity.", "Terms of the securities"),
            ),
            "entity_name is the issuer. jurisdiction is the governing-law state.",
            "Pinned investor_rights; a debt indenture is not an equity-holder rights instrument "
            "and has no better label in the six-label vocabulary (other).",
        ),
        SubclassSchema(
            "board_resolution", "Board / Stockholder Resolution or Written Consent",
            "other", "corporate_action_approval",
            "Action taken by a board, committee or stockholders (meeting or written consent).",
            _s(*_BASE, "signatories", "jurisdiction"),
            (
                _E("acting_body", "name", "Body that acted: board of directors / committee / stockholders.",
                   "Heading / first recital", True),
                _E("action_approved", "free_text", "What was resolved, one grounded sentence.",
                   "'RESOLVED' clauses", True),
                _E("resolutions", "entity_list:free_text", "Each separate resolution.", "'RESOLVED' clauses"),
            ),
            "signatories are the directors/stockholders who signed or the secretary who certifies.",
        ),
        SubclassSchema(
            "officer_certificate", "Officer / Secretary Certificate", "other", "governance_rules",
            "Certificate by an officer attesting to facts or attaching governing documents.",
            _s(*_BASE, "signatories", "jurisdiction"),
            (
                _E("certifying_officer", "name", "Officer who signs the certificate.",
                   "Signature block", True),
                _E("officer_title", "name", "That officer's title.", "Signature block", True),
                _E("certified_matter", "free_text", "What is certified (charter, bylaws, resolutions, incumbency).",
                   "Certification recital", True),
                _E("addressee", "name", "Party relying on the certificate (trustee, lender, agent).",
                   "'delivered pursuant to' recital"),
            ),
            intent_review="Pinned governance_rules; a certificate attesting resolutions is "
            "corporate_action_approval, one attesting incumbency has no better label (other).",
        ),
        SubclassSchema(
            "other", "Other governance record", "other", "other",
            "Residual: a governance record that fits none of the ten shapes above.",
            _s(*_BASE, "signatories", "jurisdiction", "filing_number"),
            (),
            "Extract only the core fields actually present.",
        ),
    )
}

# --- drift guards ----------------------------------------------------------

if set(SUBCLASS_SCHEMAS) != set(DOC_TYPE_SUBCLASSES["corporate_record"]):
    raise RuntimeError("corporate_record_schema subclasses drifted from corpus.DOC_TYPE_SUBCLASSES")
for _sc in SUBCLASS_SCHEMAS.values():
    if _sc.record_type not in RECORD_TYPES:
        raise RuntimeError(f"{_sc.subclass}: record_type {_sc.record_type!r} not in the 5-token enum")
    _dup = {e.key for e in _sc.entities} & set(CORE_FIELDS)
    if _dup:
        raise RuntimeError(f"{_sc.subclass}: entity keys shadow core fields: {sorted(_dup)}")
    # ``omit_entity_keys`` may only suppress a key the subclass (or a
    # close-alias sibling sharing its record_type) actually declares —
    # otherwise the "omit X entirely, even when null" prompt clause would
    # reference an entity the schema never knows about.
    _family = {e.key for e in _sc.entities} | {
        e.key
        for _other in SUBCLASS_SCHEMAS.values()
        if _other.record_type == _sc.record_type
        for e in _other.entities
    }
    _bad_omit = set(_sc.omit_entity_keys) - _family
    if _bad_omit:
        raise RuntimeError(
            f"{_sc.subclass}: omit_entity_keys names undeclared entities: {sorted(_bad_omit)}"
        )
    # ``core_guidance`` targets core fields this subclass actually applies;
    # guidance on a dropped core field would be dead prompt text.
    _bad_guidance = {k for k, _ in _sc.core_guidance} - set(_sc.core_applicable)
    if _bad_guidance:
        raise RuntimeError(
            f"{_sc.subclass}: core_guidance names inapplicable core fields: {sorted(_bad_guidance)}"
        )
del _sc

# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


def subclass_schema(subclass: Any) -> SubclassSchema:
    """Schema for *subclass* (normalized; unknown values fall to ``other``)."""
    from .corpus import normalize_corpus_subclass

    key = normalize_corpus_subclass("corporate_record", subclass)
    return SUBCLASS_SCHEMAS.get(key) or SUBCLASS_SCHEMAS["other"]


def record_type_for_subclass(subclass: Any) -> str:
    """The extraction ``record_type`` token a sorter subclass maps to."""
    return subclass_schema(subclass).record_type


def entity_field_types(subclass: Any) -> dict[str, str]:
    """Extension-entity field→scoring-type map for *subclass*."""
    return {e.key: e.type for e in subclass_schema(subclass).entities}


# Only these two statuses document "expected empty"; ``pending_annotation``
# means the label never ran (stale values must not score) and ``not_applicable``
# means the field is outside the subclass — neither counts as a labeled entity.
_DOCUMENTED_ABSENCE = frozenset({"schema_documented_absence", "not_applicable"})
_UNLABELED_STATUSES = frozenset({"not_applicable", "pending_annotation"})


def all_entity_field_types() -> dict[str, str]:
    """Extension-entity field→type map across every subclass."""
    out: dict[str, str] = {}
    for sch in SUBCLASS_SCHEMAS.values():
        for e in sch.entities:
            out.setdefault(e.key, e.type)
    return out


def _labeled(presence: Mapping[str, str], gt: Mapping[str, Any], key: str) -> bool:
    """Whether a field has a value or an explicit populated/absence label.

    ``not_applicable`` and ``pending_annotation`` override any stored value.
    """
    status = presence.get(key)
    if status == "populated" or status == "schema_documented_absence":
        return True
    if status in _UNLABELED_STATUSES:
        return False
    return not is_empty_value(gt.get(key))


def scored_field_types(subclass: Any, gt: Mapping[str, Any] | str | None = None) -> dict[str, str]:
    """Field→type map to score a *subclass* row against.

    Applicable core fields always; extension entities only when *gt* labels
    them (a populated value or ``gt_presence`` ``populated`` /
    ``schema_documented_absence``). Without *gt* only the core is returned.
    ``not_applicable`` and ``pending_annotation`` exclude extension fields
    even when a value is stored. Malformed *gt* raises ``TypeError`` or
    ``ValueError`` from :func:`parse_gt_fields`.
    """
    schema = subclass_schema(subclass)
    out = {f: DEFAULT_FIELD_TYPES["corporate_record"][f] for f in schema.core_applicable}
    if gt is not None:
        parsed = parse_gt_fields(gt)
        presence = gt_presence_map(parsed)
        for ent in schema.entities:
            if _labeled(presence, parsed, ent.key):
                out[ent.key] = ent.type
    return out


# ---------------------------------------------------------------------------
# unlabeled-field honesty
# ---------------------------------------------------------------------------


def unlabeled_fields(subclass: Any, gt: Mapping[str, Any] | str | None) -> tuple[str, ...]:
    """Return unlabeled entity fields from the core and the current subclass.

    A blank entity field is *expected empty* only when ``gt_presence`` says
    ``schema_documented_absence`` / ``not_applicable``. A blank with no such
    statement is **unlabeled**: the annotation never ran, so neither a filled
    nor an empty prediction may be scored against it. ``pending_annotation``
    is unlabeled even with a stored value; ``populated`` alone does not make
    a blank value labeled. Subclass applicability alone does not exclude a
    field. ``None`` means empty GT; malformed GT raises ``TypeError`` or
    ``ValueError`` from :func:`parse_gt_fields`.
    """
    schema = subclass_schema(subclass)
    parsed = parse_gt_fields(gt) if gt is not None else {}
    presence = gt_presence_map(parsed)
    candidates = [f for f in ENTITY_CORE_FIELDS] + [e.key for e in schema.entities]
    out: list[str] = []
    for key in candidates:
        status = presence.get(key)
        if status == "pending_annotation":
            out.append(key)
            continue
        if not is_empty_value(parsed.get(key)):
            continue
        if status in _DOCUMENTED_ABSENCE:
            continue
        out.append(key)
    return tuple(out)


def align_unlabeled(
    subclass: Any,
    gt: Mapping[str, Any] | str | None,
    expected: Mapping[str, Any],
    predicted: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]:
    """Drop unlabeled fields from both sides; return ``(expected, predicted, dropped)``.

    *gt* is the raw (pre-scoping) Hub metadata, so documented absences are
    still visible. Predicted extension keys the dataset does not label are
    dropped too, including keys from other subclasses. Return new dictionaries
    without mutating the inputs. ``dropped`` contains only :func:`unlabeled_fields`
    results, not every removed extension key. Malformed *gt* raises ``TypeError``
    or ``ValueError`` from :func:`parse_gt_fields`.
    """
    dropped = unlabeled_fields(subclass, gt)
    parsed = parse_gt_fields(gt) if gt is not None else {}
    presence = gt_presence_map(parsed)
    # Extension keys of *any* subclass the dataset does not label are not
    # scoring events (a stray key is a prompt-adherence matter, see
    # ``audit_gt_row``), so they never count as spurious fill.
    stray = {
        e.key
        for o in SUBCLASS_SCHEMAS.values()
        for e in o.entities
        if not _labeled(presence, parsed, e.key)
    }
    if not dropped and not (stray & set(predicted)):
        return dict(expected), dict(predicted), ()
    gone = set(dropped) | stray
    return (
        {k: v for k, v in expected.items() if k not in gone},
        {k: v for k, v in predicted.items() if k not in gone},
        dropped,
    )


# ---------------------------------------------------------------------------
# prompt + JSON Schema rendering
# ---------------------------------------------------------------------------

_TYPE_JSON: dict[str, dict[str, Any]] = {
    "name": {"type": ["string", "null"]},
    "id": {"type": ["string", "null"]},
    "date": {"type": ["string", "null"]},
    "money": {"type": ["string", "null"]},
    "label": {"type": ["string", "null"]},
    "free_text": {"type": ["string", "null"]},
}


def _json_for(field_type: str) -> dict[str, Any]:
    """Return string-array schemas for entity lists, nullable strings otherwise."""
    if field_type.startswith("entity_list"):
        return {"type": "array", "items": {"type": "string"}}
    return dict(_TYPE_JSON.get(field_type, {"type": ["string", "null"]}))


def corporate_record_json_schema(subclass: Any = None) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for one subclass; core only when ``None``.

    All declared keys are required and additional keys are rejected. A subclass
    adds its extension keys and restricts inapplicable core fields to null
    (null or an empty array for list fields). ``record_type`` accepts any
    ``RECORD_TYPES`` token or null, rather than only the subclass's mapped token.
    """
    if subclass is None:
        types = dict(DEFAULT_FIELD_TYPES["corporate_record"])
        title = "CorporateRecordExtraction"
    else:
        schema = subclass_schema(subclass)
        types = {f: DEFAULT_FIELD_TYPES["corporate_record"][f] for f in CORE_FIELDS}
        types.update({e.key: e.type for e in schema.entities})
        title = f"CorporateRecordExtraction[{schema.subclass}]"
    props = {k: _json_for(t) for k, t in types.items()}
    if subclass is not None:
        # The prompt says "null = emit null/[]": inapplicable core keys stay
        # required but may only be empty.
        for f in CORE_FIELDS:
            if f not in schema.core_applicable:
                props[f] = (
                    {"type": ["array", "null"], "maxItems": 0}
                    if types[f].startswith("entity_list") or f in ("keywords", "signatories")
                    else {"type": "null"}
                )
    if "record_type" in props:
        props["record_type"] = {"enum": [*RECORD_TYPES, None]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        "type": "object",
        "properties": props,
        "required": list(types),
        "additionalProperties": False,
    }


def render_subclass_block(subclass: Any) -> str:
    """One compact prompt line for a subclass (generated from the data).

    ``subclass[ | alias]: record_type, intent; +extra keys [] = list;
    omit A entirely, even when null; null = emit null/[]``.
    Descriptions and where-to-look hints stay in :data:`SUBCLASS_SCHEMAS`
    (docs / audit) and out of the model-visible prompt.
    """
    s = subclass_schema(subclass)
    extras = ", ".join(e.key + ("[]" if e.type.startswith("entity_list") else "") for e in s.entities)
    # Clause order below — extras, omit, core_guidance, null-per-field —
    # is FROZEN: it must match the byte-locked corporate_records_specialist_v7
    # prompt (sha256 253559fc…). Reordering breaks the byte-lock.
    line = f"- {s.subclass}: {s.record_type}, {s.intent}"
    if extras:
        line += f"; +{extras}"
    if s.omit_entity_keys:
        line += "; omit " + ", ".join(s.omit_entity_keys) + " entirely, even when null"
    if s.core_guidance:
        line += "; " + ", ".join(f"{k} = {v}" for k, v in s.core_guidance)
    if s.core_not_applicable:
        line += "; null " + ", ".join(s.core_not_applicable)
    return line


def render_prompt_section() -> str:
    """The per-subclass lines embedded in the specialist prompt.

    Every class in :data:`SUBCLASS_SCHEMAS` gets its own line — including
    ``certificate_of_formation`` (no Hub rows, but the frozen eval-environment
    prompt carries its line) — in dict insertion order with the residual
    ``other`` last. Byte-identical to the doc_subclass block of the frozen
    corporate_records_specialist_v7 prompt.
    """
    order = [k for k in SUBCLASS_SCHEMAS if k != "other"] + ["other"]
    return "\n".join(render_subclass_block(k) for k in order)


# ---------------------------------------------------------------------------
# label audit
# ---------------------------------------------------------------------------

_TEMPLATE_MARK = "(SEC EDGAR exhibit"
_ID_KEYWORD = re.compile(r"^(EX-[\w.\-()]+|\d{10}-\d{2}-\d{6}|S-1(/A)?|8-K|10-[KQ](/A)?)$", re.I)
_CIK_KEYWORD = re.compile(r"\(CIK \d+\)")


def _keywords(gt: Mapping[str, Any]) -> list[str]:
    """Return stringified keyword list items, or ``[]`` for a non-list value."""
    value = parse_json_container(gt.get("keywords"))
    return [str(v) for v in value] if isinstance(value, list) else []


#: Title cues per subclass, matched against *prose* ``subject_matter`` only
#: (template subject_matter just echoes the subclass label).
_SUBJECT_CUES: dict[str, re.Pattern[str]] = {
    "bylaws": re.compile(r"\bby-?laws\b", re.I),
    "articles_of_incorporation": re.compile(
        r"certificate of incorporation|articles of incorporation|memorandum and articles", re.I
    ),
    "powers_of_attorney": re.compile(r"power of attorney|powers of attorney", re.I),
    "rights_instrument": re.compile(
        r"\bwarrants?\b|registration rights|rights agreement|purchase option|"
        r"subscription agreement|specimen", re.I
    ),
    "indenture": re.compile(r"\bindenture\b", re.I),
    "officer_certificate": re.compile(r"(officer|secretary)'?s?'? certificate", re.I),
    "subsidiary_list": re.compile(r"\bsubsidiaries\b", re.I),
    "board_resolution": re.compile(r"\bminutes\b|\bresolutions?\b|written consent", re.I),
}


def _subject_conflicts(subclass: str, subject: str) -> list[str]:
    """Subclasses whose title cue appears in prose *subject* but not the row's own."""
    if not subject or _TEMPLATE_MARK in subject:
        return []
    own = _SUBJECT_CUES.get(subclass)
    if own is not None and own.search(subject):
        return []
    return [s for s, rx in _SUBJECT_CUES.items() if s != subclass and rx.search(subject)]


def audit_gt_row(subclass: Any, gt: Mapping[str, Any] | str) -> list[str]:
    """Label defects for one Hub row. ``[]`` means the row is clean.

    Codes: ``entity_unlabeled:<field>`` (blank with absence undocumented, or
    ``pending_annotation`` even with a stored value),
    ``subject_matter_template`` (provenance boilerplate, not a description),
    ``keywords_provenance_ids`` (exhibit/accession/CIK strings, not document
    terms), ``intent_mismatch`` (differs from the subclass pin),
    ``intent_heuristic`` (auto-labeled), ``entity_wrong_subclass:<field>``
    (populated entity from another subclass's schema),
    ``subclass_content_conflict:<subclass>`` (prose ``subject_matter`` names a
    different record shape than ``expected_subclass`` — the per-subclass schema
    would be applied to the wrong document shape).

    Malformed *gt* raises ``TypeError`` or ``ValueError`` from
    :func:`parse_gt_fields`; this function does not convert it to a defect code.
    """
    s = subclass_schema(subclass)
    parsed = parse_gt_fields(gt)
    defects = [f"entity_unlabeled:{f}" for f in unlabeled_fields(s.subclass, parsed)]
    if _TEMPLATE_MARK in str(parsed.get("subject_matter") or ""):
        defects.append("subject_matter_template")
    kws = _keywords(parsed)
    if any(_ID_KEYWORD.match(k.strip()) or _CIK_KEYWORD.search(k) for k in kws):
        defects.append("keywords_provenance_ids")
    intent = str(parsed.get("intent") or "").strip()
    if intent and intent != s.intent:
        defects.append("intent_mismatch")
    if str(parsed.get("intent_source") or "") == "heuristic":
        defects.append("intent_heuristic")
    for other in _subject_conflicts(s.subclass, str(parsed.get("subject_matter") or "")):
        defects.append(f"subclass_content_conflict:{other}")
    own = {e.key for e in s.entities}
    foreign = {e.key for o in SUBCLASS_SCHEMAS.values() for e in o.entities} - own
    for key in sorted(foreign):
        if not is_empty_value(parsed.get(key)):
            defects.append(f"entity_wrong_subclass:{key}")
    return defects


@dataclass
class _Audit:
    """Running row and per-subclass defect counts for :func:`audit_gt_rows`."""

    n_rows: int = 0
    by_subclass: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return row and per-subclass counts with totals summed across subclasses."""
        totals: dict[str, int] = {}
        for counts in self.by_subclass.values():
            for code, n in counts.items():
                totals[code] = totals.get(code, 0) + n
        return {"n_rows": self.n_rows, "totals": totals, "by_subclass": self.by_subclass}


def audit_gt_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate :func:`audit_gt_row` over Hub ground-truth rows.

    Each row needs ``expected`` (doc class), ``expected_subclass`` and
    ``gt_fields``; non-``corporate_record`` rows are skipped. Returns
    ``{"n_rows", "totals", "by_subclass"}`` with defect-code counts; the
    ``"rows"`` key counts rows in both totals and each subclass. Missing or
    unknown subclasses use ``other``. ``TypeError`` or ``ValueError`` from a
    row's audit becomes a ``gt_unparseable`` defect and processing continues.
    """
    audit = _Audit()
    for row in rows:
        if row.get("expected") != "corporate_record":
            continue
        sub = subclass_schema(row.get("expected_subclass")).subclass
        try:
            raw_gt = row.get("gt_fields")
            defects = audit_gt_row(sub, {} if raw_gt is None else raw_gt)
        except (TypeError, ValueError):
            defects = ["gt_unparseable"]
        audit.n_rows += 1
        counts = audit.by_subclass.setdefault(sub, {"rows": 0})
        counts["rows"] += 1
        for code in defects:
            counts[code] = counts.get(code, 0) + 1
    return audit.to_dict()
