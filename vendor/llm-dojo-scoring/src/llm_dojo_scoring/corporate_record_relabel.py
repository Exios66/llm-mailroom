"""Deterministic repair of ``corporate_record`` ground-truth rows.

Fixes only what can be derived from the row itself, never invented:

* ``entity_name`` — the company already named in the template ``subject_matter``
  (``"<subclass> of <Company>  (TICKER)  (CIK 0000000000) (SEC EDGAR exhibit)."``),
  with ticker/CIK stripped; marked ``populated``;
* template ``subject_matter`` → one grounded sentence naming the record and company;
* ``keywords`` — provenance strings (exhibit number, form type, accession, CIK)
  removed; the record phrase and company name kept;
* ``expected_subclass`` — re-assigned when prose ``subject_matter`` unambiguously
  names a different record shape (``subclass_content_conflict``);
* ``record_type`` — the subclass's mapped Hub token (``populated``);
* ``intent`` — re-pinned to the subclass only when ``intent_source`` is empty
  or ``heuristic``; an adjudicated source is left unchanged, and the source
  itself is never rewritten;
* ``gt_presence`` — extended with the subclass's entity fields:
  ``populated`` / ``not_applicable``; every other entity field is **left
  unlabeled** (absent from the map) for real annotation.

:func:`relabel_row` returns a new row plus a change list; it never mutates input.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .corporate_record_schema import (
    ENTITY_CORE_FIELDS,
    _ID_KEYWORD,
    _CIK_KEYWORD,
    RECORD_TYPES,
    _subject_conflicts,
    subclass_schema,
)
from .gt_metadata import gt_presence_map, is_empty_value, parse_gt_fields

__all__ = ["relabel_row", "relabel_rows", "company_from_template"]

_TEMPLATE_RE = re.compile(r"^\s*\S+ of (?P<company>.+?)\s*\(CIK\s*\d+\)\s*\(SEC EDGAR exhibit[^)]*\)", re.S)
# Trailing ``(TICK)`` / ``(TICK, TICK-PA)`` and EDGAR ``/UT/`` state suffixes are
# filer metadata, not part of the legal name. Country / entity-form codes are not.
_TICKER_RE = re.compile(r"\s*\((?:[A-Z][A-Z0-9.\-]{0,5})(?:\s*,\s*[A-Z][A-Z0-9.\-]{0,5})*\)\s*$")
_STATE_RE = re.compile(r"\s*/[A-Z]{2}/\s*$")
_NOT_TICKERS = frozenset(
    {"UK", "US", "USA", "EU", "LLC", "LP", "LLP", "LTD", "INC", "CORP", "CO", "NV", "AG", "SA", "PLC"}
)

_PHRASE = {
    "bylaws": "Bylaws",
    "articles_of_incorporation": "Articles of incorporation",
    "certificate_of_formation": "Certificate of formation",
    "charter_amendment": "Charter amendment",
    "powers_of_attorney": "Power of attorney",
    "subsidiary_list": "List of subsidiaries",
    "rights_instrument": "Rights instrument",
    "indenture": "Indenture",
    "board_resolution": "Board resolution",
    "officer_certificate": "Officer certificate",
    "other": "Corporate record",
}


def company_from_template(subject_matter: str) -> str | None:
    """Return the company from an EDGAR template, or ``None`` if absent or empty.

    Normalize whitespace and strip trailing ticker and EDGAR state suffixes.
    """
    m = _TEMPLATE_RE.match(subject_matter or "")
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group("company")).strip()
    while True:
        stripped = _STATE_RE.sub("", name).strip()
        m = _TICKER_RE.search(stripped)
        if m and not (set(re.findall(r"[A-Z0-9.\-]+", m.group(0))) & _NOT_TICKERS):
            stripped = stripped[: m.start()].strip()
        if stripped == name:
            break
        name = stripped
    return name or None


def relabel_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(new_row, changes)`` for one Hub ground-truth row without mutation.

    Repair templates and provenance keywords, resolve unambiguous subclass
    conflicts, and fill invalid record types. Preserve valid record types
    unless the subclass changes; repin intent only when ``intent_source`` is
    empty or ``heuristic``. Mark empty, inapplicable core fields as
    ``not_applicable`` unless their presence is ``populated``.

    Corporate rows serialize ``gt_fields`` and nested containers as JSON;
    other rows pass through as shallow copies. ``changes`` records label
    repairs, excluding serialization and presence-only updates. Malformed
    ``gt_fields`` raises ``TypeError`` or ``ValueError`` from :func:`parse_gt_fields`.
    """
    new = dict(row)
    changes: list[str] = []
    if row.get("expected") != "corporate_record":
        return new, changes
    raw_gt = row.get("gt_fields")
    gt = parse_gt_fields({} if raw_gt is None else raw_gt)
    presence = dict(gt_presence_map(gt))
    sub = subclass_schema(row.get("expected_subclass")).subclass

    # 1. subclass conflicts (prose subject_matter only)
    conflicts = _subject_conflicts(sub, str(gt.get("subject_matter") or ""))
    reassigned = len(conflicts) == 1
    if reassigned:
        changes.append(f"expected_subclass:{sub}->{conflicts[0]}")
        sub = conflicts[0]
        new["expected_subclass"] = sub
    schema = subclass_schema(sub)

    # 2. entity_name + subject_matter + keywords from the template
    subject = str(gt.get("subject_matter") or "")
    company = company_from_template(subject)
    if company:
        gt["entity_name"] = company
        presence["entity_name"] = "populated"
        gt["subject_matter"] = f"{_PHRASE[schema.subclass]} of {company}" + ("" if company.endswith(".") else ".")
        changes += ["entity_name", "subject_matter"]
    kws = gt.get("keywords")
    if isinstance(kws, list):
        own = sub.replace("_", " ")
        kept = [
            k for k in kws
            if not (_ID_KEYWORD.match(str(k).strip()) or _CIK_KEYWORD.search(str(k)))
        ]
        if company:
            # The grounded phrase replaces the bare subclass-name keyword.
            kept = [k for k in kept if str(k).strip().lower().replace("_", " ") != own]
            kept = [_PHRASE[schema.subclass], *kept]
            if company not in kept:
                kept.append(company)
        if kept != kws:
            gt["keywords"] = kept
            changes.append("keywords")

    # record_type: fill when blank/invalid or when the subclass was reassigned;
    # a valid existing token is a label, not a gap.
    current_rt = gt.get("record_type")
    if current_rt not in RECORD_TYPES or reassigned:
        if current_rt != schema.record_type:
            changes.append("record_type")
        gt["record_type"] = schema.record_type
    if presence.get("record_type") != "populated":
        presence["record_type"] = "populated"

    # intent: re-pin only auto-labeled intents; never override an adjudicated one.
    if str(gt.get("intent_source") or "") in ("", "heuristic") and gt.get("intent") != schema.intent:
        changes.append(f"intent:{gt.get('intent')}->{schema.intent}")
        gt["intent"] = schema.intent
    for f in ENTITY_CORE_FIELDS:
        if f in schema.core_applicable:
            continue
        if presence.get(f) == "populated" or not is_empty_value(gt.get(f)):
            continue  # a stated value is a label, whatever the subclass says
        presence[f] = "not_applicable"
    out = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in gt.items()}
    out["gt_presence"] = json.dumps(presence, sort_keys=True)
    new["gt_fields"] = json.dumps(out, sort_keys=True)
    return new, changes


def relabel_rows(rows):
    """Yield ``(new_row, changes)`` for each row in input order.

    Catch ``TypeError`` and ``ValueError`` from :func:`relabel_row`, yielding
    a shallow copy of the original row with ``["gt_unparseable"]`` instead.
    Other errors propagate.
    """
    for row in rows:
        try:
            yield relabel_row(row)
        except (TypeError, ValueError):
            # Malformed gt_fields (including a non-string/non-mapping value
            # such as a list or int): leave the row untouched so the audit
            # flags it as ``gt_unparseable`` instead of aborting the batch.
            yield dict(row), ["gt_unparseable"]
