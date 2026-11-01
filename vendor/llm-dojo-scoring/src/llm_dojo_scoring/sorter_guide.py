"""Sorter-facing subclass guide: what each subclass key means in the mailroom-dataset corpus.

``corpus.DOC_TYPE_SUBCLASSES`` is the scoring surface (every key a scorer
must normalize). A producer that hands that tuple to a sorter as a bare list
of keys gets the label conventions wrong in systematic ways. Measured on the
2,979-document ``ground_truth`` train split with ``qwen/qwen3.7-flash`` and
sorter prompt V14 (subclass 72.5% against the verified targets):

- 116 CMS carrier tables called ``outpatient`` (the carrier documents are
  "MEDICARE SUMMARY NOTICE -- PHYSICIAN/SUPPLIER CLAIM (Part B)");
- 48 Certificates of Designation called ``rights_instrument`` (the corpus
  files them under ``officer_certificate``);
- 35 Certificates of Amendment called ``certificate_of_formation`` or
  ``articles_of_incorporation`` (``certificate_of_formation`` has no
  ground-truth rows; it is a scoring key only);
- 9 board-adopted bylaw amendments called ``bylaws`` (``board_resolution``);
- 36 consulting / employment, IP and note-purchase agreements called
  ``other``;
- 364 correspondence forms missed because the corpus applies a marker
  precedence (:data:`CORRESPONDENCE_SUBCLASS_PRECEDENCE`) the bare list
  never states.

This module is the single source for the sorter catalog text: the keys a
sorter may emit (:func:`sorter_subclass_keys`), one line per key
(:data:`SORTER_SUBCLASS_GUIDE`) and the rendered block
(:func:`format_sorter_subclass_guide`). It also says which expected
subclasses are not labels at all (:data:`UNANNOTATED_SUBCLASSES`,
:func:`subclass_scorable`).
"""

from __future__ import annotations

from typing import Any

from .corpus import DOC_TYPE_SUBCLASSES, INSURANCE_CLAIM_TYPES

__all__ = [
    "CORRESPONDENCE_SUBCLASS_PRECEDENCE",
    "SORTER_EXCLUDED_SUBCLASSES",
    "SORTER_SUBCLASS_GUIDE",
    "SORTER_SUBCLASS_INSTRUCTIONS",
    "UNANNOTATED_SUBCLASSES",
    "format_sorter_subclass_guide",
    "sorter_subclass_keys",
    "subclass_scorable",
]

#: Correspondence subclass precedence of the corpus labels: the FIRST form whose
#: marker appears anywhere in the text (forwards and quoted mail included) is
#: the label; plain mail with none of them is ``email``.
CORRESPONDENCE_SUBCLASS_PRECEDENCE: tuple[str, ...] = (
    "attorney_demand",
    "letter",
    "press_release",
    "memo",
    "demand",
    "notice",
    "meeting_request",
    "email",
)

#: Scoring keys a sorter must not emit: no ground-truth row carries them and the
#: corpus labels the documents they name elsewhere (a certificate of formation is
#: ``articles_of_incorporation``).
SORTER_EXCLUDED_SUBCLASSES: dict[str, tuple[str, ...]] = {
    "corporate_record": ("certificate_of_formation",),
}

#: Expected subclass values that are not labels. Merger ``other`` marks an
#: agreement with no MAUD "Type of Consideration" annotation (every annotated
#: row's label equals its MAUD answer), so the class is scored and the
#: subclass is not.
UNANNOTATED_SUBCLASSES: dict[str, tuple[str, ...]] = {
    "merger_agreement": ("other",),
}

#: One instruction line per class, printed before its keys.
SORTER_SUBCLASS_INSTRUCTIONS: dict[str, str] = {
    "contract": (
        "the agreement family; a title naming one family decides it; hybrids take the family "
        "the operating obligations mostly concern; copy the key into contract_subtype; other "
        "only when no family fits"
    ),
    "merger_agreement": (
        "what each share of the target's COMMON stock converts into, from the conversion / "
        "Effect on Capital Stock section (not the table of contents); options, appraisal, "
        "financing and escrow do not change it"
    ),
    "corporate_record": "the record the document's own title names; an exhibit stamp never decides",
    "correspondence": (
        "the FIRST of these forms whose marker appears anywhere in the text, forwards and "
        "quoted mail included, in this order; email only when none appears"
    ),
    "insurance_claim": (
        "CMS / DE-SynPUF tables by the Medicare notice they render; other claim paperwork "
        "by line of insurance"
    ),
}

#: One line per key: the corpus convention for that label.
SORTER_SUBCLASS_GUIDE: dict[str, dict[str, str]] = {
    "contract": {
        "affiliate": "affiliate or referral programme",
        "agency": "agency or sales agency",
        "collaboration": "collaboration or cooperation agreement, incl. shareholder standstill settlements",
        "co_branding": "co-branding",
        "consulting": "consulting, employment agreement, executive offer or employment letter, and their termination",
        "development": "development work, milestones, steering committee",
        "distributor": "distribution",
        "endorsement": "endorsement, incl. insurance or annuity endorsement riders",
        "franchise": "franchise",
        "hosting": "hosting, platform or SaaS provision",
        "ip": "IP assignment, transfer or separation",
        "joint_venture": "joint venture; Rule 13d-1(k) Joint Filing Agreement",
        "license": "licence of IP, software, content or technology",
        "maintenance": "maintenance or support; licence plus support; capital or net-worth maintenance",
        "manufacturing": "manufacturing",
        "marketing": "marketing or remarketing",
        "non_compete_no_solicit": "stand-alone non-compete or non-solicit",
        "outsourcing": "outsourcing",
        "promotion": "promotion",
        "reseller": "reseller",
        "service": "services, master services, statement of work",
        "sponsorship": "sponsorship",
        "strategic_alliance": "strategic alliance",
        "supply": "supply or purchase, incl. asset purchase and note purchase agreements and their extensions",
        "transportation": "transportation, shipping or logistics",
        "other": "no family fits",
    },
    "merger_agreement": {
        "all_cash": "cash only, incl. a cash tender offer then merger",
        "all_stock": "acquirer shares only; cash in lieu of fractional shares is still all_stock",
        "mixed_cash_stock": "a fixed mix of cash and stock for every share",
        "mixed_cash_stock_election": "holders elect cash or stock, usually prorated",
        "other": "cash or stock plus a CVR, units, partnership interests or other consideration",
    },
    "corporate_record": {
        "articles_of_incorporation": (
            "certificate or articles of incorporation, restated charter, certificate of formation, "
            "articles of organization (the charter itself, even with its amendments appended)"
        ),
        "bylaws": "bylaws or amended and restated bylaws (the full rules)",
        "charter_amendment": (
            "Certificate or Articles of Amendment of a charter, Certificate of Correction "
            "(the amending certificate, not the restated charter)"
        ),
        "board_resolution": (
            "board or stockholder resolution or written consent, incl. a short amendment to the "
            "bylaws adopted by the board"
        ),
        "officer_certificate": (
            "officer's or secretary's certificate; Certificate of Designation(s) of preferred stock "
            "and its amendments (never rights_instrument)"
        ),
        "powers_of_attorney": "power of attorney",
        "subsidiary_list": "list or table of subsidiaries",
        "indenture": "indenture or supplemental indenture",
        "rights_instrument": (
            "registration, investor, stockholder or preemptive rights agreement, subscription "
            "agreement, warrant, unit purchase option, specimen stock certificate"
        ),
        "other": "none fits",
    },
    "correspondence": {
        "attorney_demand": "a demand made by or for an attorney or law firm (Esq., Law Offices, counsel for)",
        "letter": "a letter: a salutation (\"Dear ...\") with a formal closing (Sincerely, Very truly yours, Regards)",
        "press_release": "contains or discusses a press or news release (For Immediate Release, PR Newswire, Business Wire)",
        "memo": "a memorandum (TO / FROM / DATE / RE lines in the body, or named memo or memorandum)",
        "demand": "contains or discusses a demand (demand letter, demand for payment, past due, final notice, arbitration demand)",
        "notice": "contains or discusses a notice (Notice of ..., Notice No., give notice, please be notified)",
        "meeting_request": "invites or asks someone to meet or confer (meeting, call, appointment, event invitation)",
        "email": "an ordinary message with none of the markers above",
    },
    "insurance_claim": {
        "carrier": "CMS carrier table: MEDICARE SUMMARY NOTICE -- PHYSICIAN/SUPPLIER CLAIM (Part B), professional claims",
        "outpatient": "CMS outpatient table: MEDICARE SUMMARY NOTICE -- OUTPATIENT SERVICES (hospital outpatient)",
        "inpatient": "CMS inpatient table: inpatient admission, discharge, DRG",
        "pde": "CMS Part D prescription drug events",
        "auto": "auto or motor-vehicle claim",
        "property": "property, homeowner, household or building claim",
        "liability": "general, professional or product liability claim",
        "health": "health or medical claim paperwork that is not a CMS table",
        "life": "life insurance or death-benefit claim",
        "workers_comp": "workers' compensation claim",
        "other": "claim paperwork of another line, or no line stated",
    },
}


def sorter_subclass_keys(doc_type: str | None) -> tuple[str, ...]:
    """Subclass keys a sorter may emit for ``doc_type``, in catalog order.

    The scoring catalog less :data:`SORTER_EXCLUDED_SUBCLASSES`; correspondence
    in :data:`CORRESPONDENCE_SUBCLASS_PRECEDENCE` order; insurance adds the FNOL
    lines of :data:`corpus.INSURANCE_CLAIM_TYPES` the producer accepts.
    """
    kind = str(doc_type or "")
    keys = DOC_TYPE_SUBCLASSES.get(kind, ())
    if not keys:
        return ()
    excluded = set(SORTER_EXCLUDED_SUBCLASSES.get(kind, ()))
    if kind == "correspondence":
        ordered = [k for k in CORRESPONDENCE_SUBCLASS_PRECEDENCE if k in keys]
        keys = tuple(ordered + [k for k in keys if k not in ordered and k != "other"])
    elif kind == "insurance_claim":
        keys = tuple(keys) + tuple(k for k in INSURANCE_CLAIM_TYPES if k not in keys)
    elif kind == "contract" and "other" not in keys:
        keys = tuple(keys) + ("other",)
    return tuple(k for k in keys if k not in excluded)


def subclass_scorable(doc_type: str | None, expected: Any) -> bool:
    """Whether an expected subclass is a label a prediction can be scored against."""
    text = str(expected or "").strip()
    if not text:
        return False
    return text not in UNANNOTATED_SUBCLASSES.get(str(doc_type or ""), ())


def format_sorter_subclass_guide(doc_type: str | None) -> str:
    """The catalog block for one class: its instruction line, then ``key: meaning`` per key."""
    kind = str(doc_type or "")
    keys = sorter_subclass_keys(kind)
    if not keys:
        return ""
    guide = SORTER_SUBCLASS_GUIDE.get(kind, {})
    head = SORTER_SUBCLASS_INSTRUCTIONS.get(kind)
    lines = [f"- {kind}" + (f" ({head}):" if head else ":")]
    lines.extend(f"    {key}: {guide[key]}" if key in guide else f"    {key}" for key in keys)
    return "\n".join(lines)
