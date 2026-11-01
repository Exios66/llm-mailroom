"""MAUD entity-extraction schema — the machine-checkable question→answer contract.

SAND-042 scored Qwen3-8B at 2.7% MAUD accuracy. The frozen
``merger_agreement_specialist`` prompt demands ``'<Question>: <Hub valid_class>'``
but lists only the 22 *question names* — never the allowed answers — and the
output grammar was a plain array of strings. This module closes that gap by
making the contract data, not prose:

* :data:`MAUD_QUESTIONS` — all 22 Hub questions as frozen records: deal-point
  group, ``answer_type``, ``valid_classes`` (pulled verbatim from
  :mod:`llm_dojo_scoring.maud` — never invented), plain-English meaning,
  where in a merger agreement the answer lives, and a tailored specialist
  question that lists the exact allowed answers.
* :data:`MAUD_CONSIDERATION_SUBCLASSES` — the five ``subclass`` /
  ``merger_consideration`` tokens, their mapping from the MAUD
  ``Type of Consideration`` classes, and *documentation-only* entity hints.
* :data:`MergerAgreementExtraction` — the 10 scored fields, asserted equal to
  ``DEFAULT_FIELD_TYPES["merger_agreement"]`` at import time (drift fails
  loudly).
* Helpers: :func:`maud_question`, :func:`maud_questions`,
  :func:`maud_json_schema`, :func:`render_maud_question_block`,
  :func:`parse_maud_clause`, :func:`maud_answers_to_clauses`.

Everything scored stays in ``maud.py`` / ``content_scoring.py``; this module
only *imports* from them. The deal-point group and the prose explanations are
authored here (the Hub ``category`` string is not available offline) — each
question carries an ``uncertain`` flag and note where the reading of the
collapsed Hub question is not fully certain; see
:func:`category_mismatches` to audit groups against real GT records.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Sequence

from .config import (
    MAUD_CONSIDERATION_ALIASES,
    MAUD_CONSIDERATION_EQUIVALENCES,
    MAUD_CONSIDERATION_LABELS,
    MAUD_CONSIDERATION_TYPES,
)
from .corpus import (
    CORPUS_EXTRACTION_FIELDS,
    MAUD_CLAUSE_CATEGORIES,
    MAUD_QUESTION_KEYS,
)
from .maud import MAUD_ANSWER_CLASSES, MAUD_VARIABLE_CLASS_QUESTIONS
from .suites import DEFAULT_FIELD_TYPES

__all__ = [
    "MAUD_CONSIDERATION_SUBCLASSES",
    "MAUD_DEAL_POINT_GROUPS",
    "MAUD_LINES_KEY",
    "MAUD_OBJECT_KEY",
    "MAUD_QUESTIONS",
    "MAUD_TYPE_OF_CONSIDERATION_TO_TOKEN",
    "ConsiderationSubclass",
    "EntityField",
    "EntityHint",
    "EntitySchema",
    "MaudQuestion",
    "MergerAgreementExtraction",
    "category_mismatches",
    "consideration_subclass",
    "maud_answers_to_clauses",
    "maud_clause_line",
    "maud_clauses_to_answers",
    "maud_json_schema",
    "maud_json_schema_text",
    "maud_question",
    "maud_questions",
    "parse_maud_clause",
    "render_maud_question_block",
    "scorer_ambiguous_classes",
    "scorer_fold_collisions",
]

AnswerType = Literal["single_choice", "multi_label"]
Shape = Literal["lines", "object"]

#: Extraction key the scorer actually reads (``peel_non_extraction_fields``
#: falls back to ``maud_clauses`` for the prediction side). Either a list of
#: ``'<Question>: <class>'`` strings or a ``{question: class}`` mapping parses.
MAUD_LINES_KEY = "maud_clauses"
#: Key used by the ``shape="object"`` grammar. The scorer does **not** read
#: it — run :func:`maud_answers_to_clauses` to produce ``maud_clauses`` first.
MAUD_OBJECT_KEY = "maud_answers"

#: MAUD's seven deal-point groups (``corpus.MAUD_CLAUSE_CATEGORIES``), in the
#: order used by the reference docs.
MAUD_DEAL_POINT_GROUPS: tuple[str, ...] = (
    "General Information",
    "Conditions to Closing",
    "Material Adverse Effect",
    "Knowledge",
    "Operating and Efforts Covenant",
    "Deal Protection and Related Provisions",
    "Remedies",
)

# ---------------------------------------------------------------------------
# Text normalization (verbatim-preserving; deliberately NOT maud._fold, which
# strips punctuation and so folds "> 5 business days" onto "5 business days").
# ---------------------------------------------------------------------------

_SMART_QUOTES = str.maketrans(
    {"“": '"', "”": '"', "‘": "'", "’": "'"}
)
_TYPO = re.compile(r"fundermental", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+")


def _squash(text: object) -> str:
    """Smart quotes → ASCII and whitespace collapsed (case preserved)."""
    return " ".join(str(text or "").translate(_SMART_QUOTES).split())


def _qkey(text: object) -> str:
    """Normalize question lookup text with folded case, quotes, and whitespace."""
    return _squash(text).lower()


def _akey(text: object) -> str:
    """Normalize answer lookup text, also correcting the known fundamental typo."""
    return _TYPO.sub("fundamental", _squash(text)).lower()


# ---------------------------------------------------------------------------
# Question records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaudQuestion:
    """One of the 22 Hub MAUD questions."""

    name: str
    group: str
    answer_type: AnswerType
    valid_classes: tuple[str, ...]
    #: True for the four questions whose per-row class set varies;
    #: ``valid_classes`` is then the corpus *union* (a superset of any row).
    variable: bool
    explanation: str
    sections: tuple[str, ...]
    #: Tailored specialist question with the exact allowed answers inline.
    question: str
    #: True when the reading of the (collapsed) Hub question or its group is
    #: not fully certain; ``note`` says why.
    uncertain: bool = False
    note: str = ""

    @property
    def prompt_classes(self) -> tuple[str, ...]:
        """``valid_classes`` minus scorer-equivalent typo duplicates.

        The Hub ships both ``fundamental/Special`` and ``Fundermental/Special``
        spellings; the scorer folds them. Prompts show only the first.
        """
        seen: set[str] = set()
        out: list[str] = []
        for cls in self.valid_classes:
            key = _akey(cls)
            if key not in seen:
                seen.add(key)
                out.append(cls)
        return tuple(out)

    @property
    def composite_classes(self) -> tuple[str, ...]:
        """Classes that extend another class of this question by ``", <more>"``."""
        return tuple(
            cls
            for cls in self.valid_classes
            if any(cls != other and cls.startswith(other + ", ") for other in self.valid_classes)
        )

    def class_for(self, answer: object) -> str | None:
        """Verbatim catalog class for *answer* (quote/space/case-insensitive)."""
        return _class_index(self.name).get(_akey(answer))

    def clause(self, answer: str) -> str:
        """``'<Question>: <class>'`` line; raises ``ValueError`` off-vocabulary."""
        return maud_clause_line(self.name, answer)


def _derive_answer_type(classes: Sequence[str]) -> AnswerType:
    """``multi_label`` iff a class is another class plus ``", <more>"``.

    That is exactly the shape :func:`llm_dojo_scoring.maud.canonical_maud_class`
    handles component-wise (``_components``): the Hub stores a multi-label
    answer as one comma-joined string drawn from ``valid_classes``.
    """
    for cls in classes:
        if any(cls != other and cls.startswith(other + ", ") for other in classes):
            return "multi_label"
    return "single_choice"


def _class_index(name: str) -> dict[str, str]:
    """Map normalized answer keys to their first catalog spelling for a question."""
    index: dict[str, str] = {}
    for cls in MAUD_ANSWER_CLASSES[name]:
        index.setdefault(_akey(cls), cls)
    return index


# name -> (group, explanation, sections, uncertain note ("" = confident))
_SPEC: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "Absence of Litigation Closing Condition": (
        "Conditions to Closing",
        "Does a closing condition require that no litigation be pending or threatened "
        "against the deal, and how broad is it: only government actions, or private "
        "suits too; pending only, threatened, or threatened in writing?",
        ("Article VII/VI Conditions to Closing (No Injunction; No Litigation)",),
        "The Hub classes mix two axes (who sues: governmental vs any; how: pending "
        "vs threatened vs threatened in writing). Reading is from the class names.",
    ),
    "Accuracy of Target R&W Closing Condition": (
        "Conditions to Closing",
        "How accurate must the Target's representations and warranties be at closing "
        "for the buyer to be obliged to close (the bring-down standard: all respects, "
        "all material respects, MAE standard, with de minimis exceptions), at what "
        "time they are measured (signing and closing, or closing only), and which "
        "R&Ws are treated as fundamental/specified (authority, capitalization, "
        "brokers, takeover statutes, ...)?",
        (
            "Article VII/VI Conditions to Obligations of Parent and Merger Sub "
            "(Representations and Warranties)",
            "Target representations (Article III/IV) for the fundamental R&W list",
        ),
        "The Hub question collapses several MAUD sub-questions (bring-down standard, "
        "bring-down timing, list of fundamental R&Ws) into one key; its per-row class "
        "set is one of 7 variants and most classes are comma-joined lists.",
    ),
    "Agreement provides for matching rights in connection with COR": (
        "Deal Protection and Related Provisions",
        "If the Target board wants to change its recommendation (COR = change of "
        "recommendation) because of a competing proposal or intervening event, does "
        "the buyer get a matching right, and how long is the match period?",
        ("No Solicitation / Change of Recommendation covenant (Article VI/V)",),
        "",
    ),
    "Agreement provides for matching rights in connection with FTR": (
        "Deal Protection and Related Provisions",
        "If the Target wants to terminate to accept a superior offer (FTR = fiduciary "
        "termination right), does the buyer get a matching right, and how long is "
        "the match period?",
        (
            "No Solicitation / Superior Proposal termination covenant (Article VI/V)",
            "Termination article (Article VIII/IX), termination for a Superior Proposal",
        ),
        "",
    ),
    "Breach of Meeting Covenant": (
        "Deal Protection and Related Provisions",
        "Does the Target's breach of its covenant to hold the stockholder meeting "
        "trigger the termination fee (or the buyer's termination right)?",
        (
            "Termination article, termination fee triggers (Article VIII/IX)",
            "Stockholders Meeting / Proxy covenant",
        ),
        "Exact trigger semantics (fee vs termination right) are inferred from the "
        "MAUD deal-point name; class set is only Yes/No.",
    ),
    "Breach of No Shop": (
        "Deal Protection and Related Provisions",
        "Does the Target's breach of the no-shop (no-solicitation) covenant trigger "
        "the termination fee (or the buyer's termination right)?",
        (
            "Termination article, termination fee triggers (Article VIII/IX)",
            "No Solicitation covenant (Article VI/V)",
        ),
        "Exact trigger semantics are inferred from the deal-point name; Yes/No only. "
        "Group could alternatively be read as Remedies.",
    ),
    "Compliance with Covenant Closing Condition": (
        "Conditions to Closing",
        "Which covenants must the Target have complied with for the buyer to be "
        "obliged to close: all covenants, each covenant, or a hybrid/other standard?",
        ("Article VII/VI Conditions to Obligations of Parent and Merger Sub (Covenants)",),
        "The Hub class names (All Covenants / Each Covenant / Hybrid/Other Standard) "
        "are terse; the precise MAUD sub-question wording is not available offline.",
    ),
    "FTR Triggers": (
        "Deal Protection and Related Provisions",
        "On what events may the Target exercise its fiduciary termination right: a "
        "Superior Offer only, or also an Intervening Event?",
        ("No Solicitation / Change of Recommendation covenant", "Termination article"),
        "",
    ),
    "Fiduciary exception to COR covenant": (
        "Deal Protection and Related Provisions",
        "What standard of fiduciary duty must the Target board meet to be allowed to "
        "change its recommendation (COR): e.g. failure would be 'inconsistent with', "
        "a 'breach' or 'violation' of, or 'reasonably likely' to breach, its "
        "fiduciary duties?",
        ("Change of Recommendation covenant within No Solicitation (Article VI/V)",),
        "Classes also include Yes/No/None, so some rows record mere existence of the exception.",
    ),
    "Fiduciary exception:  Board determination (no-shop)": (
        "Deal Protection and Related Provisions",
        "What determination must the Target board make before it may talk to or "
        "furnish information to an unsolicited bidder despite the no-shop (the "
        "fiduciary exception to the no-solicitation covenant)? NOTE the question "
        "name has two spaces after the first colon.",
        ("No Solicitation covenant, exceptions for unsolicited proposals (Article VI/V)",),
        "",
    ),
    "General Antitrust Efforts Standard": (
        "Operating and Efforts Covenant",
        "What efforts standard must the parties use to obtain antitrust / regulatory "
        "clearance: reasonable best efforts, commercially reasonable efforts, or a "
        "flat (absolute) standard?",
        ("Efforts / Regulatory Approvals covenant (Article VI/V, 'Filings; Other Actions')",),
        "",
    ),
    "Intervening Event Definition": (
        "Deal Protection and Related Provisions",
        "How does the definition of 'Intervening Event' treat events known (or "
        "reasonably foreseeable) at signing: must it arise after signing, may it be "
        "known but with unknown consequences, and so on?",
        (
            "Definitions - 'Intervening Event' (Article I or the Change of "
            "Recommendation covenant)",
        ),
        "Classes also include Yes/No (existence of a definition); mixing of the two is inferred.",
    ),
    "Knowledge Definition": (
        "Knowledge",
        "How is 'Knowledge' of the Target defined: actual knowledge only, "
        "constructive knowledge, knowledge based on a named role/people, or after "
        "reasonable investigation or inquiry?",
        ("Definitions - 'Knowledge' (Article I or Section 9.x / 'Certain Definitions')",),
        "",
    ),
    "Limitations on FTR Exercise": (
        "Deal Protection and Related Provisions",
        "What limits the Target's right to terminate for a superior offer: e.g. it "
        "may not do so if it breached the no-shop (any, material, or resulting in the "
        "Superior Offer) or breached other provisions of the agreement?",
        ("Termination for a Superior Proposal (Article VIII/IX)", "No Solicitation covenant"),
        "Multi-label: answers combine a no-shop-breach class with an 'other "
        "provisions' class in one comma-joined string.",
    ),
    "MAE Definition": (
        "Material Adverse Effect",
        "How is 'Material Adverse Effect' defined: the forward-looking standard "
        "('would'/'could' reasonably be expected to), the relational words used "
        "('resulting from', 'arising from/out of', 'relating to', 'attributable "
        "to'), what the effect must be measured against (Target and subsidiaries "
        "'taken as a whole'), which events are carved out (war or terrorism, natural "
        "disaster, announcement/pendency/consummation), and whether carveouts apply "
        "to all or only some MAE prongs?",
        ("Definitions - 'Company Material Adverse Effect' / 'Material Adverse Effect'",),
        "The Hub question collapses many MAUD MAE sub-questions into one key; its "
        "per-row class set is one of 5 variants (40 classes in the union).",
    ),
    "Negative interim operating covenant": (
        "Operating and Efforts Covenant",
        "Does the Target have negative interim operating covenants (things it may not "
        "do between signing and closing without consent), do they apply to all or only "
        "specified actions, and what is the consent standard (flat consent, or not "
        "unreasonably withheld, conditioned or delayed)?",
        ("Conduct of Business Pending the Merger (Article V/VI), 'Negative covenants' list",),
        "Hub classes merge existence, scope, and consent-standard readings.",
    ),
    "No-Shop": (
        "Deal Protection and Related Provisions",
        "Does the agreement prohibit the Target from soliciting competing offers (a "
        "no-shop), and is the Target's responsibility for its representatives' "
        "breaches judged on a reasonable standard or strict liability?",
        ("No Solicitation / Acquisition Proposals covenant (Article VI/V)",),
        "Yes/No existence is merged with the Reasonable standard / Strict liability "
        "reading of responsibility for representatives; inferred from class names.",
    ),
    "Ordinary course covenant": (
        "Operating and Efforts Covenant",
        "Must the Target conduct its business in the ordinary course between signing "
        "and closing, and with what standard: a flat covenant, commercially "
        "reasonable efforts, reasonable best efforts, or buyer consent not to be "
        "unreasonably withheld?",
        ("Conduct of Business Pending the Merger (Article V/VI), 'Ordinary Course'",),
        "",
    ),
    "Specific Performance": (
        "Remedies",
        "Do the parties get specific performance of the agreement, and is the right "
        "phrased as 'entitled to' specific performance or only 'entitled to seek' it?",
        ("Specific Performance (Article IX/X, miscellaneous / remedies)",),
        "",
    ),
    "Superior Offer Definition": (
        "Deal Protection and Related Provisions",
        "What portion of the Target's equity or assets must a competing proposal "
        "cover to count as a Superior Offer/Proposal: 50%, more than 50% but not "
        "'all or substantially all', less than 50%, or 'all or substantially all'?",
        ("Definitions - 'Superior Proposal' / 'Superior Offer'",),
        "",
    ),
    "Tail Period & Acquisition Proposal Details": (
        "Deal Protection and Related Provisions",
        "For the Target termination fee 'tail': how long after termination does a "
        "competing deal still trigger the fee (6, 9, 12 months or longer), and which "
        "triggers require the Acquisition Proposal to have been publicly disclosed "
        "(breach, no-vote/failure to obtain approval, outside date)?",
        ("Termination Fee / Effect of Termination (Article VIII/IX), tail provision",),
        "The Hub question collapses tail length and 'publicly disclosed' sub-questions "
        "(per-row class set is one of 2 variants).",
    ),
    "Type of Consideration": (
        "General Information",
        "What is the form of merger consideration paid to the Target's stockholders: "
        "all cash, all stock, a cash/stock mix, or a cash/stock mix with a "
        "stockholder election?",
        ("Article II Effect on Capital Stock / 'Merger Consideration' (conversion of shares)",),
        "",
    ),
}


def _tailored_question(
    name: str, explanation: str, classes: Sequence[str], variable: bool
) -> str:
    """Render question-specific instructions listing exact allowed answers and abstention."""
    options = "\n".join(f"  - {cls}" for cls in classes)
    vary = (
        "\nThe allowed set varies by agreement: pick the listed answer that fits "
        "best; do not write anything that is not on the list."
        if variable
        else ""
    )
    return (
        f"{name}: {explanation}\n"
        "Answer with EXACTLY ONE of the following, copied verbatim (including quotes, "
        f"commas and capitalization):\n{options}{vary}\n"
        "If the operative clause is not visible in this text, give no answer "
        "(omit it / null) -- never write 'not specified'."
    )


def _build_questions() -> tuple[MaudQuestion, ...]:
    """Build ordered question records from the catalog classes and curated specifications."""
    out: list[MaudQuestion] = []
    for name in MAUD_QUESTION_KEYS:
        group, explanation, sections, note = _SPEC[name]
        classes = tuple(MAUD_ANSWER_CLASSES[name])
        variable = name in MAUD_VARIABLE_CLASS_QUESTIONS
        probe = MaudQuestion(
            name=name, group=group, answer_type=_derive_answer_type(classes),
            valid_classes=classes, variable=variable, explanation=explanation,
            sections=sections, question="", uncertain=bool(note), note=note,
        )
        question = _tailored_question(name, explanation, probe.prompt_classes, variable)
        out.append(
            MaudQuestion(
                name=name, group=group, answer_type=probe.answer_type,
                valid_classes=classes, variable=variable, explanation=explanation,
                sections=sections, question=question, uncertain=bool(note), note=note,
            )
        )
    return tuple(out)


#: All 22 questions in ``MAUD_QUESTION_KEYS`` (alphabetical Hub) order.
MAUD_QUESTIONS: tuple[MaudQuestion, ...] = _build_questions()
_BY_KEY: dict[str, MaudQuestion] = {_qkey(q.name): q for q in MAUD_QUESTIONS}


def maud_question(name: str) -> MaudQuestion:
    """Look up a question; case, whitespace and smart-quote insensitive.

    ``"fiduciary exception: board determination (no-shop)"`` (single space)
    resolves to the double-space Hub name. Unknown names raise ``KeyError``.
    """
    try:
        return _BY_KEY[_qkey(name)]
    except KeyError:
        raise KeyError(f"unknown MAUD question {name!r}") from None


# ---------------------------------------------------------------------------
# Consideration subclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityHint:
    """Optional documentation for what to look for. NEVER scored."""

    name: str
    description: str
    scored: bool = False


@dataclass(frozen=True)
class ConsiderationSubclass:
    """One ``doc_subclass`` / ``merger_consideration`` token."""

    token: str
    label: str
    #: The MAUD ``Type of Consideration`` Hub class this token maps from
    #: (``None`` for ``other``: the Hub has no such class).
    maud_class: str | None
    definition: str
    #: Tokens scored as family-equivalent by ``subclass_equivalent``.
    equivalent_to: tuple[str, ...]
    #: Questions the frozen prompt's executive brief prioritizes for this subclass.
    priority_questions: tuple[str, ...]
    entity_hints: tuple[EntityHint, ...]


#: Hub ``Type of Consideration`` class → subclass token.
MAUD_TYPE_OF_CONSIDERATION_TO_TOKEN: dict[str, str] = {
    "All Cash": "all_cash",
    "All Stock": "all_stock",
    "Mixed Cash/Stock": "mixed_cash_stock",
    "Mixed Cash/Stock: Election": "mixed_cash_stock_election",
}

_HINTS: dict[str, tuple[EntityHint, ...]] = {
    "all_cash": (
        EntityHint("per_share_cash_amount", "Cash paid per Target share, e.g. '$45.00 in cash'."),
        EntityHint("cvr", "Any contingent value right issued alongside the cash."),
    ),
    "all_stock": (
        EntityHint("exchange_ratio", "Parent shares per Target share (fixed or floating/collar)."),
        EntityHint("cash_in_lieu_of_fractional_shares", "Fractional-share cash-out mechanics."),
    ),
    "mixed_cash_stock": (
        EntityHint("per_share_cash_amount", "Cash component per Target share."),
        EntityHint("exchange_ratio", "Stock component: Parent shares per Target share."),
    ),
    "mixed_cash_stock_election": (
        EntityHint("election_mechanics", "Election form, election deadline, default if no election."),
        EntityHint("proration", "Proration / oversubscription adjustment of cash vs stock."),
        EntityHint("per_share_cash_amount", "Cash amount per share for cash electors."),
        EntityHint("exchange_ratio", "Exchange ratio for stock electors."),
    ),
    "other": (
        EntityHint("consideration_description", "Non-standard consideration (e.g. units, notes, CVR only)."),
        EntityHint("cvr", "Contingent value rights, if any."),
    ),
}

_SUBCLASS_DEFS: dict[str, tuple[str, tuple[str, ...]]] = {
    "all_cash": (
        "Every Target share converts into a fixed cash amount only.",
        (
            "Type of Consideration",
            "Absence of Litigation Closing Condition",
            "MAE Definition",
            "No-Shop",
            "Ordinary course covenant",
        ),
    ),
    "all_stock": (
        "Every Target share converts into Parent stock at an exchange ratio; no cash except for fractional shares.",
        (
            "Type of Consideration",
            "Knowledge Definition",
            "Accuracy of Target R&W Closing Condition",
        ),
    ),
    "mixed_cash_stock": (
        "Each Target share converts into a fixed combination of cash and Parent stock.",
        (
            "Type of Consideration",
            "FTR Triggers",
            "Compliance with Covenant Closing Condition",
        ),
    ),
    "mixed_cash_stock_election": (
        "Mixed cash and stock where each holder may elect the form, subject to proration.",
        (
            "Type of Consideration",
            "Limitations on FTR Exercise",
            "Superior Offer Definition",
            "Tail Period & Acquisition Proposal Details",
        ),
    ),
    "other": (
        "Non-standard or unstated consideration; do not force a cash/stock token when the agreement is silent or ambiguous.",
        (),
    ),
}


def _build_subclasses() -> tuple[ConsiderationSubclass, ...]:
    """Build consideration subclasses with catalog classes, equivalences, and entity hints."""
    class_by_token = {v: k for k, v in MAUD_TYPE_OF_CONSIDERATION_TO_TOKEN.items()}
    out: list[ConsiderationSubclass] = []
    for token in MAUD_CONSIDERATION_TYPES:
        definition, priority = _SUBCLASS_DEFS[token]
        out.append(
            ConsiderationSubclass(
                token=token,
                label=MAUD_CONSIDERATION_LABELS[token],
                maud_class=class_by_token.get(token),
                definition=definition,
                equivalent_to=tuple(sorted(MAUD_CONSIDERATION_EQUIVALENCES.get(token, ()))),
                priority_questions=priority,
                entity_hints=_HINTS[token],
            )
        )
    return tuple(out)


MAUD_CONSIDERATION_SUBCLASSES: tuple[ConsiderationSubclass, ...] = _build_subclasses()


def consideration_subclass(value: object) -> ConsiderationSubclass:
    """Resolve a token, Hub ``Type of Consideration`` class, or alias.

    Unknown values raise ``ValueError`` (unlike the scorer's
    ``normalize_maud_consideration``, which degrades them to ``other``).
    """
    folded = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    for sub in MAUD_CONSIDERATION_SUBCLASSES:
        if folded == sub.token.replace("_", " "):
            return sub
    token = MAUD_CONSIDERATION_ALIASES.get(folded)
    if token is None:
        raise ValueError(f"unknown merger consideration subclass {value!r}")
    return next(s for s in MAUD_CONSIDERATION_SUBCLASSES if s.token == token)


def maud_questions(subclass: str | None = None) -> list[MaudQuestion]:
    """Ordered question list; with *subclass*, that subclass's priority questions first.

    The remaining questions keep ``MAUD_QUESTION_KEYS`` order. Nothing is
    dropped: priority comes from the frozen prompt's executive brief, which
    says what to look at first, not what to skip.
    """
    if subclass is None:
        return list(MAUD_QUESTIONS)
    sub = consideration_subclass(subclass)
    first = [maud_question(n) for n in sub.priority_questions]
    rest = [q for q in MAUD_QUESTIONS if q not in first]
    return first + rest


# ---------------------------------------------------------------------------
# Entity schema descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityField:
    """An extraction field definition with its type, empty value, and description."""
    name: str
    type: str
    #: ``None`` (unstated scalar) or ``[]`` (unstated list), per the frozen prompt.
    empty: Any
    description: str


@dataclass(frozen=True)
class EntitySchema:
    """An ordered extraction field schema associated with a document type."""
    name: str
    doc_type: str
    fields: tuple[EntityField, ...]

    @property
    def field_types(self) -> dict[str, str]:
        """Return field names mapped to their declared scoring types in schema order."""
        return {f.name: f.type for f in self.fields}

    def empty_prediction(self) -> dict[str, Any]:
        """Build an empty prediction with fresh lists and None for scalar fields."""
        return {f.name: ([] if f.empty == [] else None) for f in self.fields}


MergerAgreementExtraction = EntitySchema(
    name="MergerAgreementExtraction",
    doc_type="merger_agreement",
    fields=(
        EntityField("document_name", "name", None, "Agreement title as stated."),
        EntityField(
            "parties", "entity_list:name", [],
            "Parent, Merger Sub, Target and any other contracting entity, as written.",
        ),
        EntityField(
            "effective_date", "date", None,
            "Calendar date YYYY-MM-DD only; null when only an Effective Time is defined.",
        ),
        EntityField(
            "effective_time", "free_text", None,
            "Clock / time zone / defined-term Effective Time as written.",
        ),
        EntityField("governing_law", "name", None, "Governing-law sentence; no forum/venue."),
        EntityField(
            "merger_consideration", "name", None,
            "Exactly one token: " + ", ".join(MAUD_CONSIDERATION_TYPES) + ".",
        ),
        EntityField(
            "maud_clauses", "entity_list:free_text", [],
            "Answered MAUD questions as '<Question>: <Hub valid_class>'; omit unanswered.",
        ),
        EntityField("intent", "name", None, "One short label, e.g. effect_merger, amend_merger."),
        EntityField("subject_matter", "free_text", None, "One grounded sentence."),
        EntityField("keywords", "entity_list:name", [], "Up to 8 phrases copied from the text."),
    ),
)


def _parity_problems() -> list[str]:
    """Report merger-agreement field type, order, and count mismatches with live schemas."""
    problems: list[str] = []
    live = DEFAULT_FIELD_TYPES["merger_agreement"]
    mine = MergerAgreementExtraction.field_types
    if mine != live:
        problems.append(f"field types differ from DEFAULT_FIELD_TYPES: {mine} != {live}")
    if tuple(mine) != tuple(CORPUS_EXTRACTION_FIELDS["merger_agreement"]):
        problems.append("field order differs from CORPUS_EXTRACTION_FIELDS")
    if len(mine) != 10:
        problems.append(f"expected 10 scored fields, found {len(mine)}")
    return problems


_problems = _parity_problems()
if _problems:  # drift must fail loudly at import, not silently in a prompt
    raise AssertionError("; ".join(_problems))
del _problems

# Same loud-failure rule for the catalog this module mirrors.
assert tuple(q.name for q in MAUD_QUESTIONS) == tuple(MAUD_QUESTION_KEYS)
assert set(MAUD_DEAL_POINT_GROUPS) == set(MAUD_CLAUSE_CATEGORIES)
assert all(q.group in MAUD_DEAL_POINT_GROUPS for q in MAUD_QUESTIONS)


# ---------------------------------------------------------------------------
# Clause lines
# ---------------------------------------------------------------------------


def maud_clause_line(question: str, answer: str) -> str:
    """Render ``'<Question>: <class>'`` using the verbatim Hub strings."""
    q = maud_question(question)
    cls = q.class_for(answer)
    if cls is None:
        sub = _consideration_class(q.name, answer)
        if sub is None:
            raise ValueError(f"{answer!r} is not a valid class for {q.name!r}")
        cls = sub
    return f"{q.name}: {cls}"


def _consideration_class(name: str, answer: object) -> str | None:
    """Resolve a known consideration answer to its MAUD class, or None."""
    if name != "Type of Consideration":
        return None
    try:
        sub = consideration_subclass(answer)
    except ValueError:
        return None
    return sub.maud_class


def parse_maud_clause(line: str) -> tuple[str, str] | None:
    """Parse ``'<Question>: <answer>'`` → ``(question, verbatim class)``.

    Tolerates bullets, surrounding quotes/backticks, case, smart quotes and
    extra/missing spaces in the question name (including the Hub's double
    space in ``Fiduciary exception:  Board determination (no-shop)``). Returns
    ``None`` when the question is unknown or the answer is not one of the
    question's catalog classes (for ``Type of Consideration`` the subclass
    tokens/aliases are accepted and mapped to the Hub class).

    This is stricter than the scorer on purpose: anything it accepts the
    scorer parses to the same question and an equivalent answer
    (see ``tests/test_maud_schema.py``); the reverse does not hold.
    """
    text = _BULLET.sub("", str(line or "")).strip().strip("`").strip()
    if len(text) > 1 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1].strip()  # whole line wrapped in quotes (names never start with one)
    squashed = _squash(text)
    lowered = squashed.lower()
    for key in sorted(_BY_KEY, key=len, reverse=True):
        if lowered.startswith(key + ":"):
            question = _BY_KEY[key]
            answer = squashed[len(key) + 1:].strip().strip("`")
            for candidate in (answer, answer.rstrip(".").strip()):
                cls = question.class_for(candidate) or _consideration_class(
                    question.name, candidate
                )
                if cls is not None:
                    return question.name, cls
            return None
    return None


def maud_answers_to_clauses(
    answers: Mapping[str, object] | None, *, strict: bool = False
) -> list[str]:
    """``{question: class | None}`` → scorer-ready ``maud_clauses`` lines.

    ``None`` / blank answers are dropped before clause rendering. When inputs
    go directly to the scorer, a top-level ``{question: None}`` entry is
    unanswered; a blank string or a record with ``answer: None`` is present
    with an empty answer and fails class validation.
    With ``strict=True`` unknown questions / off-vocabulary answers raise
    ``ValueError``; otherwise they pass through unvalidated so the scorer sees
    exactly what the model said.
    """
    lines: list[str] = []
    for question, answer in (answers or {}).items():
        if answer is None or str(answer).strip() == "":
            continue
        try:
            lines.append(maud_clause_line(question, str(answer)))
        except (KeyError, ValueError):
            if strict:
                raise ValueError(f"invalid MAUD answer {question!r}: {answer!r}") from None
            lines.append(f"{question}: {answer}")
    return lines


def maud_clauses_to_answers(lines: Iterable[str]) -> dict[str, str]:
    """Inverse of :func:`maud_answers_to_clauses` for valid lines (others skipped)."""
    out: dict[str, str] = {}
    for line in lines:
        parsed = parse_maud_clause(line)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


def scorer_fold_collisions() -> dict[str, list[tuple[str, ...]]]:
    """Classes the scorer's punctuation-stripping fold cannot tell apart.

    ``{question: [(class, class, ...), ...]}`` using
    ``llm_dojo_scoring.maud._fold``. Only intended equivalences remain: the
    ``Fundermental`` typo twins and ``'> 5 business days'`` ≡ ``'Greater
    than 5 business days'`` (the fold spells comparators out, so ``'> 5'``
    no longer collapses onto ``'5 business days'``).
    """
    from .maud import _fold

    out: dict[str, list[tuple[str, ...]]] = {}
    for question in MAUD_QUESTIONS:
        groups: dict[str, list[str]] = {}
        for cls in question.valid_classes:
            groups.setdefault(_fold(cls), []).append(cls)
        dup = [tuple(v) for v in groups.values() if len(v) > 1]
        if dup:
            out[question.name] = dup
    return out


def scorer_ambiguous_classes() -> dict[str, tuple[str, ...]]:
    """Catalog classes the scorer's GT-ambiguity detector would mis-flag.

    ``detect_maud_gt_ambiguity`` splits answers on whitespace-delimited ``/``,
    ``|`` or ``;`` (``scorecard_honesty._COLLAPSED_ANSWER_SPLIT``). A single
    legitimate class such as ``"... No-Vote / MTC Failure Trigger"`` therefore
    makes a row *unscorable* on that question. ``{question: classes}``.
    """
    from .scorecard_honesty import _split_collapsed_answers

    out: dict[str, tuple[str, ...]] = {}
    for question in MAUD_QUESTIONS:
        hit = tuple(c for c in question.valid_classes if len(_split_collapsed_answers(c)) > 1)
        if hit:
            out[question.name] = hit
    return out


def category_mismatches(labels: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Audit this module's groups against real GT ``maud_clause_labels``.

    Returns ``[(question, ours, gt_category), ...]`` for records whose
    ``category`` is non-empty and differs from :attr:`MaudQuestion.group`.
    """
    out: list[tuple[str, str, str]] = []
    for question, rec in labels.items():
        try:
            ours = maud_question(question).group
        except KeyError:
            continue
        gt = str(rec.get("category") or "") if isinstance(rec, Mapping) else ""
        if gt and gt != ours:
            out.append((question, ours, gt))
    return out


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------


def render_maud_question_block(
    subclass: str | None = None,
    *,
    shape: Shape = "object",
    questions: Sequence[str] | None = None,
    chunked: bool = True,
) -> str:
    """Prompt-ready block: every question with every allowed answer verbatim.

    ``shape`` selects the output instruction (``"object"`` →
    ``maud_answers`` mapping with ``null`` for unanswered; ``"lines"`` →
    ``maud_clauses`` strings, unanswered omitted). ``chunked`` adds the
    chunk duty: merger documents are processed in several chunks, so a
    question is answered only when its operative clause is visible in the
    text at hand. *subclass* puts that subclass's priority questions first.
    """
    if shape not in ("lines", "object"):
        raise ValueError(f"unknown shape {shape!r}")
    ordered = maud_questions(subclass)
    if questions is not None:
        wanted = [maud_question(n) for n in questions]
        ordered = [q for q in ordered if q in wanted]
    total = len(ordered)
    if shape == "object":
        fmt = (
            f'Output format: "{MAUD_OBJECT_KEY}" is a JSON object whose keys are the '
            "question names below, copied exactly, and whose values are ONE allowed "
            "answer string, or null when the question is not answered by this text."
        )
        unanswered = "set its value to null"
    else:
        fmt = (
            f'Output format: "{MAUD_LINES_KEY}" is a list of strings, one per answered '
            'question, each exactly "<Question>: <Answer>" (question name, colon, one '
            "space, answer)."
        )
        unanswered = "omit it from the list"
    lines = [
        f"MAUD QUESTIONS ({total}) - choose from the allowed answers; do not paraphrase",
        "",
        "For every question you answer, choose EXACTLY ONE of its allowed answers and "
        "copy it character for character (quotes, commas, slashes, colons, "
        "capitalization). Never paraphrase, abbreviate, translate, or invent an "
        "answer. An answer that looks like several joined items, e.g. "
        '"Resulting from", "Arising from/out of", is ONE allowed answer: copy the '
        "whole string, with its items in the listed order.",
    ]
    if chunked:
        lines.append(
            "Chunk duty: you may be shown only part of the agreement. Answer a "
            "question only if its operative clause is visible in THIS text; "
            f"otherwise {unanswered}. Never guess, and never write 'not specified'."
        )
    lines += [fmt]
    if any(q.name == "Fiduciary exception:  Board determination (no-shop)" for q in ordered):
        lines.append(
            "Note: the question name 'Fiduciary exception:  Board determination "
            "(no-shop)' has TWO spaces after its first colon; copy it exactly."
        )
    sub = consideration_subclass(subclass) if subclass is not None else None
    for idx, q in enumerate(ordered, 1):
        lines += ["", f"[{idx}/{total}] {q.name}  ({q.group})"]
        flag = (
            f" [priority for {sub.token} deals]"
            if sub is not None and q.name in sub.priority_questions
            else ""
        )
        lines.append(f"Asks{flag}: {q.explanation}")
        lines.append("Usually found in: " + "; ".join(q.sections))
        head = "Allowed answers (pick exactly one):"
        if q.variable:
            head = (
                "Allowed answers (the set varies by agreement; this is the union - "
                "pick exactly one that fits):"
            )
        lines.append(head)
        lines += [f"  - {cls}" for cls in q.prompt_classes]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON Schema for guided decoding
# ---------------------------------------------------------------------------


def _scalar_schema(field_type: str) -> dict[str, Any]:
    """Build nullable string JSON Schema, restricting dates to ISO-shaped strings."""
    if field_type == "date":
        return {"type": ["string", "null"], "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    return {"type": ["string", "null"]}


def maud_json_schema(
    shape: Shape = "object",
    *,
    full: bool = True,
    questions: Sequence[str] | None = None,
    row_classes: Mapping[str, Sequence[str]] | None = None,
    include_trace: bool = True,
) -> dict[str, Any]:
    """JSON Schema (2020-12) for vLLM guided decoding.

    ``shape="object"`` (recommended): ``maud_answers`` is
    ``{question: enum[valid_classes] | null}`` with every question required, so
    the model must make one decision per question, can only emit a catalog
    string, and *cannot* mis-type a question name (the double-space name is a
    schema key, not text to reproduce). ``shape="lines"``: ``maud_clauses`` is
    an array of ``enum`` strings ``"<Question>: <class>"`` (every
    question x class pair, 265 strings / ~25 KB for the union catalog).

    Why ``enum`` and not a regex ``pattern`` for lines: both have the same size
    (one alternative per pair), but ``enum`` compiles to literal alternatives
    with no regex escaping (the classes contain ``( ) " / & ' |``-adjacent
    punctuation and curly quotes), and failure cannot hide in a mis-escaped
    metacharacter. Grammar size stays sane because every class string is a
    closed literal that appears once per question and no part of the grammar
    is free-form (no ``.*``).

    Limits of the lines grammar: JSON Schema cannot say "at most one line per
    question"; ``uniqueItems`` and ``maxItems`` only bound it. Prefer ``object``.

    ``row_classes`` narrows chosen questions to a row's own ``valid_classes``
    (the right thing for the four variable-class questions when the GT record
    is known); ``questions`` restricts which questions appear. ``full=False``
    returns just the MAUD sub-schema (what goes under the MAUD key).
    """
    chosen = [maud_question(n) for n in questions] if questions is not None else list(MAUD_QUESTIONS)
    narrowed: dict[str, list[str]] = {}
    for key, classes in (row_classes or {}).items():
        q = maud_question(key)
        verbatim = [c for c in (q.class_for(c) for c in classes) if c is not None]
        if len(verbatim) != len(list(classes)):
            raise ValueError(f"row_classes for {q.name!r} contain off-catalog classes")
        narrowed[q.name] = verbatim

    def classes_of(q: MaudQuestion) -> list[str]:
        """Return narrowed answer classes when available, otherwise the full question catalog."""
        return narrowed.get(q.name, list(q.valid_classes))

    if shape == "object":
        sub: dict[str, Any] = {
            "type": "object",
            "properties": {q.name: {"enum": [*classes_of(q), None]} for q in chosen},
            "required": [q.name for q in chosen],
            "additionalProperties": False,
        }
        key = MAUD_OBJECT_KEY
    elif shape == "lines":
        pairs = [f"{q.name}: {cls}" for q in chosen for cls in classes_of(q)]
        sub = {
            "type": "array",
            "items": {"enum": pairs},
            "uniqueItems": True,
            "maxItems": len(chosen),
        }
        key = MAUD_LINES_KEY
    else:
        raise ValueError(f"unknown shape {shape!r}")
    if not full:
        return sub

    props: dict[str, Any] = {}
    if include_trace:
        props["reasoning"] = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "evidence": {"type": "string"},
                            "section_ref": {"type": ["string", "null"]},
                        },
                        "required": ["field", "evidence", "section_ref"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "entries"],
            "additionalProperties": False,
        }
    for f in MergerAgreementExtraction.fields:
        if f.name == "maud_clauses":
            props[key] = sub
        elif f.name == "merger_consideration":
            props[f.name] = {"enum": [*MAUD_CONSIDERATION_TYPES, None]}
        elif f.type.startswith("entity_list"):
            props[f.name] = {"type": "array", "items": {"type": "string"}}
        else:
            props[f.name] = _scalar_schema(f.type)
    if include_trace:
        props["confidence"] = {"type": "number", "minimum": 0.0, "maximum": 1.0}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MergerAgreementExtraction",
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def maud_json_schema_text(*args: Any, **kwargs: Any) -> str:
    """:func:`maud_json_schema` serialized compactly (vLLM ``guided_json``)."""
    return json.dumps(maud_json_schema(*args, **kwargs), ensure_ascii=False, separators=(",", ":"))
