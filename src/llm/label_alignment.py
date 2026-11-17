"""Opt-in label-aligned guidance for the specialist prompts (A/B experiment arm).

The frozen v1 specialist prompts (``llm/frozen_v1``) are pinned by content and
stay byte-identical. This module adds guidance on top of them so experiments can
measure whether prompts that state the ground-truth labeling rules score better
against the interim and near_gold labels:

* ``rules``: field definitions, closed vocabularies and edge-case rules copied
  from the gold-label guide (local-mailroom-sandbox
  ``docs/gold-labels/LABELING_GUIDE.md``), which the near_gold catalog follows.
* ``rules_few_shot``: the rules plus short worked examples. The examples are
  synthetic texts that reproduce cases settled by reviewed (near_gold) labels;
  no corpus document is quoted, so no eval document leaks into the prompt.

Resolution, first match wins: ``MAILROOM_PROMPT_ALIGNMENT`` (``off``, ``rules``
or ``rules_few_shot``; an empty or unknown value is ignored), taxonomy
``prompt_alignment.mode``, ``off``. Off is the default, so production prompts are
unchanged unless a run opts in. Each addendum is Langfuse-managed like any other
prompt (``mailroom-<template key>``, registered in ``prompt_templates()``) with
this module's text as the local fallback.
"""

from __future__ import annotations

import os

ALIGNMENT_ENV = "MAILROOM_PROMPT_ALIGNMENT"
MODES: tuple[str, ...] = ("off", "rules", "rules_few_shot")
SEPARATOR = "\n\n"


def _party_roles() -> str:
    from llm_dojo_scoring.schemas.cuad_schema import CONTRACT_PARTY_ROLES

    return ", ".join(CONTRACT_PARTY_ROLES)


CORRESPONDENCE_RULES = """Label-aligned rules (these match how the ground-truth labels are written; where they are more specific than the rules above, follow these):

Which message the fields describe. The document's own header is often stripped, so the message the document IS may have no header. Take sender, recipient, additional_recipients and communication_date from the TOP message:
- A reply typed above quoted mail (a note with its own salutation, content or signature above the first header): its author (the signature) is the sender and its salutation is the recipient. additional_recipients is [] and communication_date is null unless the reply itself states them. The quoted message's people and date are NOT this document's sender, recipient or date.
- An unsigned top note directly above a "Forwarded by X on <date>" stamp: X is the sender and the stamp's date is the send date.
- A bare forward (no note, or only "FYI" / "see below"): use the first message that has its own header (a From:/To:/Sent: or Date: block, or a sender name and date line followed by To:, as Lotus Notes prints it). A "Forwarded by" line alone is not a header.
- A signature alone ("Thanks, Jim") is not a header, but when the document has no header at all, the signature's name is the sender.
- A calendar entry's sender is its chairperson or organiser; its "Date:" is the event date, not a send date.

Field definitions:
- recipient: the first To: entry as printed (a person, a distribution list or a group such as "All Enron Houston"), else a salutation that names a person or a defined group ("Dear GISB Members", "Houston Attendees:"). A generic salutation ("Dear Friends", "Gentlemen", "All", "To Our Readers") is not a named addressee: null. When several people are in To:, the first is recipient and the rest go in additional_recipients.
- additional_recipients: further To: entries plus cc'd or copied parties, as written.
- communication_date: YYYY-MM-DD. Never a deadline, meeting date or invoice date.
- demand_amount: only money the sender demands that the RECIPIENT pay. An amount that is only reported (an invoice total in an internal follow-up, money owed by someone else, an amount the sender will sue a third party for) is null.
- action_items: at most 3 concrete actions the reader is asked to take, with deadlines if stated. A direct question or request put to the reader counts. A bare pointer ("please see the attached notice", "check it out") does not. A request in a quoted message that the reader made of someone else does not.
- urgency: routine (neutral or unspecified), time-sensitive (a near deadline or a dated request), urgent (stated urgency such as "ASAP", "immediately", "urgent" aimed at the reader), critical (legal, financial or safety consequences stated as imminent). A meeting reminder or standing invitation is routine; it is time-sensitive only when it asks the reader to act or confirm today, tomorrow or by a stated near date.

Promotional or bulk mail (newsletters, fare alerts, store offers, event ticket announcements): action_items is [] and urgency is routine. Offer deadlines, countdowns and a shouted "URGENT" in a mass mailing are sales pressure, not pressure on the reader."""

CORRESPONDENCE_EXAMPLES = """Worked examples (synthetic texts; each reproduces a case settled by reviewed labels):

Example 1 — reply typed above quoted mail.
Text:
  Pat, I can cover the Thursday call. Lee
  -----Original Message-----
  From: Morgan, Pat
  Sent: Tuesday, March 6, 2001 9:14 AM
  To: Quinn, Lee
  Subject: Thursday call
  Can you cover the Thursday call for me?
Fields: sender "Lee"; recipient "Pat"; additional_recipients []; communication_date null; action_items [].
(Not sender "Morgan, Pat": that is the quoted message.)

Example 2 — unsigned note above a forward stamp.
Text:
  Please review before Friday.
  ---------------------- Forwarded by Dana Ross/HOU/ECT on 05/14/2001 10:02 AM ---------------------------
  From: Avery Cole  To: Dana Ross ...
Fields: sender "Dana Ross"; communication_date "2001-05-14"; action_items ["Review the forwarded material before Friday"].

Example 3 — promotional mail.
Text:
  LAST CHANCE! Fares from $49 end Sunday. Book now at ...
Fields: action_items []; urgency "routine".

Example 4 — defined-group salutation.
Text:
  Dear ICAP Working Group Members: The next session moves to Room 4B ...
Fields: recipient "ICAP Working Group Members". (A generic "Dear Friends" would be null.)"""

CORPORATE_RULES = """Label-aligned rules (these match how the ground-truth labels are written; where they are more specific than the rules above, follow these):
- entity_name: the corporation or entity the record is about, full legal name as written.
- effective_date: the date the record takes effect; when no effective date is stated, its adoption or filing date. YYYY-MM-DD.
- signatories: the people who sign the record (officers, directors, incorporators, grantors) as printed in the execution block. [] when there is no execution block.
- jurisdiction: the state or country under whose law the entity is organised ("a Delaware corporation" -> "Delaware"). Prefer that over a governing-law clause.
- For a long record, look for these anchors before declaring a field absent: "IN WITNESS WHEREOF", "By:", "/s/", "Name:", "dated as of", "effective", "day of", "organized under", "incorporated under", "State of"."""

CONTRACTS_RULES_TEMPLATE = """Label-aligned rules (these match how the ground-truth labels are written; where they are more specific than the rules above, follow these). Fill these fields as well:
- agreement_date: the "dated as of" or signature date, YYYY-MM-DD.
- expiration_date: the end of the initial term when the text states it as a date, YYYY-MM-DD; else null. Never compute it from a term length.
- signatories: the persons who sign the contract, as printed in the signature blocks. [] when none are printed.
- party_roles: one "<party>: <role>" line per party, the party named as in parties. role is exactly one of: {roles}. Use other only when no listed role fits.
- termination_notice_period: the notice needed to terminate the contract or to stop a renewal, as stated ("90 days' written notice").
- contract_value: the monetary consideration as a plain currency phrase as written ("$2,000,000"). $0 is a stated amount. Never computed.
- dispute_forum: the courts or arbitral seat for disputes, kept separate from governing_law.
- payment_terms: how and when the consideration is paid, as stated ("net 30 days from invoice"). Null when no payment mechanics are stated.
- territory: the stated territory of the grant, distribution or service.
- For a long contract, look for these anchors before declaring a field absent: "IN WITNESS WHEREOF", "By:", "/s/", "Name:", "dated as of", "terminate", "notice", "term", "territory", "fee", "payment", "arbitration", "courts of"."""


def _contracts_rules() -> str:
    return CONTRACTS_RULES_TEMPLATE.replace("{roles}", _party_roles())


# agent -> ((template key, fallback text factory, minimum mode), ...), in prompt order.
_ADDENDA: dict[str, tuple[tuple[str, object, str], ...]] = {
    "correspondence_specialist": (
        ("correspondence_specialist-label-rules", lambda: CORRESPONDENCE_RULES, "rules"),
        ("correspondence_specialist-label-examples", lambda: CORRESPONDENCE_EXAMPLES, "rules_few_shot"),
    ),
    "corporate_records_specialist": (
        ("corporate_records_specialist-label-rules", lambda: CORPORATE_RULES, "rules"),
    ),
    "contracts_specialist": (
        ("contracts_specialist-label-rules", _contracts_rules, "rules"),
    ),
}


def alignment_mode() -> str:
    """The active mode: the env var, then taxonomy ``prompt_alignment.mode``, then ``off``."""
    raw = os.environ.get(ALIGNMENT_ENV, "").strip().lower()
    if raw in MODES:
        return raw
    from pipeline.config import load_config

    block = load_config().get("prompt_alignment")
    if isinstance(block, dict):
        value = str(block.get("mode", "")).strip().lower()
        if value in MODES:
            return value
    return "off"


def alignment_templates() -> dict[str, str]:
    """Template key -> local fallback text, for ``prompt_templates()``."""
    return {key: factory() for addenda in _ADDENDA.values() for key, factory, _ in addenda}


def apply_label_alignment(agent_name: str, base_prompt: str, mode: str | None = None) -> str:
    """Return ``base_prompt`` with the addenda the active mode enables for this agent.

    ``off`` (the default), or an agent without addenda, returns ``base_prompt`` unchanged.
    """
    mode = alignment_mode() if mode is None else mode
    if mode not in MODES or mode == "off":
        return base_prompt
    level = MODES.index(mode)
    from llm.prompts import get_managed_prompt

    parts = [base_prompt.rstrip()]
    for key, factory, minimum in _ADDENDA.get(agent_name, ()):
        if level >= MODES.index(minimum):
            text, _ = get_managed_prompt(key, factory())
            parts.append(text.strip())
    return SEPARATOR.join(parts) if len(parts) > 1 else base_prompt
