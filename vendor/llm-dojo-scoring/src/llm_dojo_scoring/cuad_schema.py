"""CUAD contract entity-extraction schema — categories, families, questions.

Single authoritative, importable catalog for the ``contract`` document class
(``ContractExtraction`` / ``contracts_specialist``). It ties together four
surfaces that previously lived in separate places:

* the **41 Atticus clause categories** (:data:`CUAD_CATEGORIES`) with their
  official CUAD v1 category description, the CUAD-style lawyer-review
  question, a concise specialist-prompt question, and the scored extraction
  field a category feeds when there is one;
* the **25 contract families + ``other``** (:data:`CUAD_FAMILIES`) with the
  CUAD folder surfaces that normalize to each, a short definition, the
  characteristically salient (high-prior) categories, and optional
  family-specific entity hints;
* the **11 scored ContractExtraction fields** (:data:`CONTRACT_EXTRACTION_FIELDS`)
  with scoring type, empty value, and description;
* helpers to look categories/families up, order questions per family, render a
  prompt-ready question block, and emit a JSON Schema for guided decoding.

Authority and invariants (checked at import — a mismatch raises
``RuntimeError`` rather than silently drifting):

* category names are exactly :data:`llm_dojo_scoring.corpus.CUAD_CLAUSE_CATEGORIES`
  (the ``cuad_clause_labels`` / ``cuad_clauses`` surface) as a set; the tuple
  order here is the CUAD / frozen-prompt order, not alphabetical;
* family keys are exactly ``corpus.DOC_TYPE_SUBCLASSES["contract"]`` and the
  folder surfaces cover ``corpus.CORPUS_SUBCLASS_SURFACES["contract"]``;
* the field descriptor reproduces ``DEFAULT_FIELD_TYPES["contract"]`` exactly.

Documentation/prompt aids only: nothing here changes what is scored.
``family_fields`` hints, ``group`` / ``answer_format`` metadata and the
specialist questions are never added to the scored field map, the presence
scorer, or the frozen ``contracts_specialist`` prompt. The CUAD question text
and descriptions are reproduced from the CUAD v1 release
(``category_descriptions.csv``); the specialist questions, groups, family
definitions, salient sets and family hints are this package's own curation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .config import CONTRACT_SUBTYPE_LABELS, SUBTYPE_UNKNOWN
from .corpus import (
    CORPUS_SUBCLASS_SURFACES,
    CUAD_CLAUSE_CATEGORIES,
    DOC_TYPE_SUBCLASSES,
)
from .equivalences import normalize_subtype
from .suites import DEFAULT_FIELD_TYPES

__all__ = [
    "CUAD_ANSWER_FORMATS",
    "CUAD_CATEGORIES",
    "CUAD_CATEGORY_NAMES",
    "CUAD_FAMILIES",
    "CUAD_FAMILY_KEYS",
    "CUAD_GROUPS",
    "CUAD_QUESTION_TEMPLATE",
    "CONTRACT_EXTRACTION_FIELDS",
    "CONTRACT_FIELD_TYPES",
    "CuadCategory",
    "CuadFamily",
    "CuadField",
    "FamilyField",
    "canonical_cuad_category",
    "cuad_category",
    "cuad_family",
    "cuad_json_schema",
    "cuad_questions",
    "render_cuad_question_block",
    "salient_categories",
]

#: Answer formats. ``span`` = the CUAD answer is the extracted value itself
#: (a name, party list, date, duration, governing jurisdiction). ``yes_no`` =
#: presence of the clause is the answer (the highlighted span is evidence).
CUAD_ANSWER_FORMATS: tuple[str, ...] = ("span", "yes_no")

#: Category groups, in display order. The grouping is this package's own
#: convenience; the CUAD paper's grouping is coarser.
CUAD_GROUPS: tuple[str, ...] = (
    "basic_info",
    "term_and_termination",
    "restrictive_covenants",
    "ip_and_license",
    "commercial_terms",
    "liability_and_risk",
    "other",
)

#: CUAD's question wording. ``{name}`` is the Atticus category and
#: ``{description}`` its official category description.
CUAD_QUESTION_TEMPLATE = (
    'Highlight the parts (if any) of this contract related to "{name}" that '
    "should be reviewed by a lawyer. Details: {description}"
)


@dataclass(frozen=True)
class CuadCategory:
    """One Atticus clause category.

    ``name`` is the exact string used in ``cuad_clause_labels`` and as the
    ``'<Category>: <span>'`` prefix of ``cuad_clauses`` items. ``field`` is the
    scored ContractExtraction field the same evidence also feeds (``None`` for
    the pure presence categories). ``description`` is the official CUAD v1
    "Details" text; ``prompt_question`` is the concise specialist question.
    """

    name: str
    group: str
    answer_format: str
    description: str
    prompt_question: str
    field: str | None = None

    @property
    def question(self) -> str:
        """CUAD-style lawyer-review question for this category."""
        return CUAD_QUESTION_TEMPLATE.format(
            name=self.name, description=self.description
        )

    @property
    def is_span(self) -> bool:
        """True when the CUAD answer is an extracted value, not a yes/no."""
        return self.answer_format == "span"


# Order is the CUAD / frozen-prompt order (basic info, restrictive covenants,
# revenue/commercial risk, IP & license, liability). Descriptions are the
# CUAD v1 category descriptions.
CUAD_CATEGORIES: tuple[CuadCategory, ...] = (
    CuadCategory(
        "Document Name", "basic_info", "span",
        "The name of the contract",
        "What is the contract's own title (e.g. 'Web Hosting Agreement')?",
        field="document_name",
    ),
    CuadCategory(
        "Parties", "basic_info", "span",
        "The two or more parties who signed the contract",
        "Who are the contracting parties (full legal name plus defined alias)?",
        field="parties",
    ),
    CuadCategory(
        "Agreement Date", "basic_info", "span",
        "The date of the contract",
        "What is the date the contract was made or signed?",
        field="effective_date",
    ),
    CuadCategory(
        "Effective Date", "basic_info", "span",
        "The date when the contract is effective",
        "On what date does the contract take effect (a defined Effective Date wins over the signature date)?",
        field="effective_date",
    ),
    CuadCategory(
        "Expiration Date", "term_and_termination", "span",
        "On what date will the contract's initial term expire?",
        "When does the initial term expire, or how long does the agreement last?",
        field="term_length",
    ),
    CuadCategory(
        "Renewal Term", "term_and_termination", "span",
        "What is the renewal term after the initial term expires? This includes automatic extensions and unilateral extensions with prior notice.",
        "What is the renewal or extension term after the initial term (including evergreen rollover)?",
        field="renewal_terms",
    ),
    CuadCategory(
        "Notice Period To Terminate Renewal", "term_and_termination", "span",
        "What is the notice period required to terminate renewal?",
        "How much notice must be given to stop the contract renewing?",
        field="renewal_terms",
    ),
    CuadCategory(
        "Governing Law", "basic_info", "span",
        "Which state/country's law governs the interpretation of the contract?",
        "Which state or country's law governs the contract?",
        field="governing_law",
    ),
    CuadCategory(
        "Most Favored Nation", "commercial_terms", "yes_no",
        "Is there a clause that if a third party gets better terms on the licensing or sale of technology/goods/services described in the contract, the buyer of such technology/goods/services under the contract shall be entitled to those better terms?",
        "Does the buyer or licensee get any better terms a third party is later given?",
    ),
    CuadCategory(
        "Competitive Restriction Exception", "restrictive_covenants", "yes_no",
        "This category includes the exceptions or carveouts to Non-Compete, Exclusivity and No-Solicit of Customers above.",
        "Are there exceptions or carve-outs to the non-compete, exclusivity or customer no-solicit restrictions?",
    ),
    CuadCategory(
        "Non-Compete", "restrictive_covenants", "yes_no",
        "Is there a restriction on the ability of a party to compete with the counterparty or operate in a certain geography or business or technology sector?",
        "Is a party restricted from competing with the other, or from operating in a geography, business or technology sector?",
    ),
    CuadCategory(
        "Exclusivity", "restrictive_covenants", "yes_no",
        'Is there an exclusive dealing commitment with the counterparty? This includes a commitment to procure all "requirements" from one party of certain technology, goods, or services or a prohibition on licensing or selling technology, goods or services to third parties, or a prohibition on collaborating or working with other parties), whether during the contract or after the contract ends (or both).',
        "Is there an exclusive-dealing, requirements or no-third-party-sales commitment, during or after the term?",
    ),
    CuadCategory(
        "No-Solicit Of Customers", "restrictive_covenants", "yes_no",
        "Is a party restricted from contracting or soliciting customers or partners of the counterparty, whether during the contract or after the contract ends (or both)?",
        "Is a party barred from soliciting or contracting with the other's customers or partners?",
    ),
    CuadCategory(
        "No-Solicit Of Employees", "restrictive_covenants", "yes_no",
        "Is there a restriction on a party's soliciting or hiring employees and/or contractors from the counterparty, whether during the contract or after the contract ends (or both)?",
        "Is a party barred from soliciting or hiring the other's employees or contractors?",
    ),
    CuadCategory(
        "Non-Disparagement", "restrictive_covenants", "yes_no",
        "Is there a requirement on a party not to disparage the counterparty?",
        "Must a party refrain from disparaging the other party?",
    ),
    CuadCategory(
        "Termination For Convenience", "term_and_termination", "yes_no",
        "Can a party terminate this contract without cause (solely by giving a notice and allowing a waiting period to expire)?",
        "Can a party terminate without cause on notice and a waiting period?",
    ),
    CuadCategory(
        "Rofr/Rofo/Rofn", "restrictive_covenants", "yes_no",
        "Is there a clause granting one party a right of first refusal, right of first offer or right of first negotiation to purchase, license, market, or distribute equity interest, technology, assets, products or services?",
        "Does a party hold a right of first refusal, first offer or first negotiation?",
    ),
    CuadCategory(
        "Change Of Control", "other", "yes_no",
        "Does one party have the right to terminate or is consent or notice required of the counterparty if such party undergoes a change of control, such as a merger, stock sale, transfer of all or substantially all of its assets or business, or assignment by operation of law?",
        "Is there a termination right, consent or notice requirement if a party undergoes a change of control?",
    ),
    CuadCategory(
        "Anti-Assignment", "other", "yes_no",
        "Is consent or notice required of a party if the contract is assigned to a third party?",
        "Is consent or notice required before the contract is assigned to a third party?",
    ),
    CuadCategory(
        "Revenue/Profit Sharing", "commercial_terms", "yes_no",
        "Is one party required to share revenue or profit with the counterparty for any technology, goods, or services?",
        "Must a party share revenue or profit (royalty, commission, split) with the other?",
    ),
    CuadCategory(
        "Price Restrictions", "commercial_terms", "yes_no",
        "Is there a restriction on the ability of a party to raise or reduce prices of technology, goods, or services provided?",
        "Is a party restricted from raising or reducing prices?",
    ),
    CuadCategory(
        "Minimum Commitment", "commercial_terms", "yes_no",
        "Is there a minimum order size or minimum amount or units per-time period that one party must buy from the counterparty under the contract?",
        "Is there a minimum order size or minimum purchase amount per period?",
    ),
    CuadCategory(
        "Volume Restriction", "commercial_terms", "yes_no",
        "Is there a fee increase or consent requirement, etc. if one party's use of the product/services exceeds certain threshold?",
        "Is there a fee increase or consent requirement if usage exceeds a threshold?",
    ),
    CuadCategory(
        "Ip Ownership Assignment", "ip_and_license", "yes_no",
        "Does intellectual property created by one party become the property of the counterparty, either per the terms of the contract or upon the occurrence of certain events?",
        "Does IP created by one party become the other party's property?",
    ),
    CuadCategory(
        "Joint Ip Ownership", "ip_and_license", "yes_no",
        "Is there any clause providing for joint or shared ownership of intellectual property between the parties to the contract?",
        "Is any IP jointly or co-owned by the parties?",
    ),
    CuadCategory(
        "License Grant", "ip_and_license", "yes_no",
        "Does the contract contain a license granted by one party to its counterparty?",
        "Does one party grant the other a license?",
    ),
    CuadCategory(
        "Non-Transferable License", "ip_and_license", "yes_no",
        "Does the contract limit the ability of a party to transfer the license being granted to a third party?",
        "Is the licensee barred from transferring or sublicensing the license to a third party?",
    ),
    CuadCategory(
        "Affiliate License-Licensor", "ip_and_license", "yes_no",
        "Does the contract contain a license grant by affiliates of the licensor or that includes intellectual property of affiliates of the licensor?",
        "Does the license come from, or cover IP of, the licensor's affiliates?",
    ),
    CuadCategory(
        "Affiliate License-Licensee", "ip_and_license", "yes_no",
        "Does the contract contain a license grant to a licensee (incl. sublicensor) and the affiliates of such licensee/sublicensor?",
        "Does the license extend to the licensee's (or sublicensor's) affiliates?",
    ),
    CuadCategory(
        "Unlimited/All-You-Can-Eat-License", "ip_and_license", "yes_no",
        'Is there a clause granting one party an "enterprise," "all you can eat" or unlimited usage license?',
        "Is there an enterprise, 'all you can eat' or unlimited-usage license?",
    ),
    CuadCategory(
        "Irrevocable Or Perpetual License", "ip_and_license", "yes_no",
        "Does the contract contain a license grant that is irrevocable or perpetual?",
        "Is any license irrevocable or perpetual?",
    ),
    CuadCategory(
        "Source Code Escrow", "ip_and_license", "yes_no",
        "Is one party required to deposit its source code into escrow with a third party, which can be released to the counterparty upon the occurrence of certain events (bankruptcy, insolvency, etc.)?",
        "Must source code be placed in escrow for release on bankruptcy or similar events?",
    ),
    CuadCategory(
        "Post-Termination Services", "term_and_termination", "yes_no",
        "Is a party subject to obligations after the termination or expiration of a contract, including any post-termination transition, payment, transfer of IP, wind-down, last-buy, or similar obligations?",
        "Are there obligations after termination or expiration (transition, wind-down, last-buy, payment, IP transfer)?",
    ),
    CuadCategory(
        "Audit Rights", "commercial_terms", "yes_no",
        "Does a party have the right to audit the books, records, or physical locations of the counterparty to ensure compliance with the contract?",
        "Can a party audit the other's books, records or premises?",
    ),
    CuadCategory(
        "Uncapped Liability", "liability_and_risk", "yes_no",
        "Is a party's liability uncapped upon the breach of its obligation in the contract? This also includes uncap liability for a particular type of breach such as IP infringement or breach of confidentiality obligation.",
        "Is any liability uncapped (in general or for a breach type such as IP infringement or confidentiality)?",
    ),
    CuadCategory(
        "Cap On Liability", "liability_and_risk", "yes_no",
        "Does the contract include a cap on liability upon the breach of a party's obligation? This includes time limitation for the counterparty to bring claims or maximum amount for recovery.",
        "Is liability capped, or is there a time limit or maximum amount for claims?",
    ),
    CuadCategory(
        "Liquidated Damages", "liability_and_risk", "yes_no",
        "Does the contract contain a clause that would award either party liquidated damages for breach or a fee upon the termination of a contract (termination fee)?",
        "Are liquidated damages or a termination fee payable?",
    ),
    CuadCategory(
        "Warranty Duration", "liability_and_risk", "span",
        "What is the duration of any warranty against defects or errors in technology, products, or services provided under the contract?",
        "How long does the warranty against defects or errors last?",
    ),
    CuadCategory(
        "Insurance", "liability_and_risk", "yes_no",
        "Is there a requirement for insurance that must be maintained by one party for the benefit of the counterparty?",
        "Must a party maintain insurance for the other's benefit?",
    ),
    CuadCategory(
        "Covenant Not To Sue", "restrictive_covenants", "yes_no",
        "Is a party restricted from contesting the validity of the counterparty's ownership of intellectual property or otherwise bringing a claim against the counterparty for matters unrelated to the contract?",
        "Is a party barred from challenging the other's IP ownership or suing it on unrelated matters?",
    ),
    CuadCategory(
        "Third Party Beneficiary", "other", "yes_no",
        "Is there a non-contracting party who is a beneficiary to some or all of the clauses in the contract and therefore can enforce its rights against a contracting party?",
        "Can a non-party beneficiary enforce any of the contract's clauses?",
    ),
)

#: Atticus category names in CUAD order.
CUAD_CATEGORY_NAMES: tuple[str, ...] = tuple(c.name for c in CUAD_CATEGORIES)


@dataclass(frozen=True)
class FamilyField:
    """Optional family-specific entity hint (documentation / prompt aid).

    Never scored and never part of the emitted JSON. ``informs`` names the
    scored ContractExtraction field the hint helps populate (``None`` when it
    is context only), e.g. a royalty rate informs ``contract_value``.
    """

    name: str
    description: str
    informs: str | None = None


@dataclass(frozen=True)
class CuadFamily:
    """One CUAD contract family (subclass) as the sorter emits it.

    ``key`` is the canonical snake_case ``cuad_family`` value; ``surfaces`` are
    the CUAD folder names that normalize to it via
    :func:`llm_dojo_scoring.equivalences.normalize_subtype`. ``salient`` lists
    the categories characteristically present (high-prior) for the family, in
    priority order — a prompt prior, not a presence label.
    """

    key: str
    label: str
    surfaces: tuple[str, ...]
    definition: str
    salient: tuple[str, ...]
    family_fields: tuple[FamilyField, ...] = ()


def _ff(name: str, description: str, informs: str | None = None) -> FamilyField:
    """Construct a family-specific field with an optional scored-field association."""
    return FamilyField(name, description, informs)


CUAD_FAMILIES: tuple[CuadFamily, ...] = (
    CuadFamily(
        "affiliate", CONTRACT_SUBTYPE_LABELS["affiliate"],
        ("Affiliate_Agreements",),
        "Affiliate or referral-program agreement: a publisher or partner promotes the counterparty's products for a commission or referral fee.",
        ("Affiliate License-Licensee", "Affiliate License-Licensor", "License Grant",
         "Non-Transferable License", "Revenue/Profit Sharing", "Termination For Convenience",
         "Exclusivity", "Anti-Assignment", "Governing Law", "Renewal Term"),
        (_ff("commission_rate", "Commission or referral fee rate", "contract_value"),
         _ff("program_territory", "Territory or channels the affiliate may promote in")),
    ),
    CuadFamily(
        "agency", CONTRACT_SUBTYPE_LABELS["agency"],
        ("Agency Agreements",),
        "Agency agreement: one party (agent) represents or sells for the other (principal), usually for a commission.",
        ("Termination For Convenience", "Governing Law", "Notice Period To Terminate Renewal",
         "Exclusivity", "Non-Compete", "Revenue/Profit Sharing", "Anti-Assignment",
         "Renewal Term", "Expiration Date"),
        (_ff("principal_and_agent", "Which party is principal and which is agent"),
         _ff("commission_rate", "Commission rate or schedule", "contract_value"),
         _ff("territory", "Territory the agent covers")),
    ),
    CuadFamily(
        "collaboration", CONTRACT_SUBTYPE_LABELS["collaboration"],
        ("Collaboration",),
        "Collaboration or cooperation agreement: parties pool R&D or other effort toward a shared program.",
        ("Joint Ip Ownership", "Ip Ownership Assignment", "License Grant", "Exclusivity",
         "Revenue/Profit Sharing", "Termination For Convenience", "Expiration Date",
         "Governing Law", "Non-Compete", "Change Of Control"),
        (_ff("collaboration_scope", "Program or field of collaboration"),
         _ff("cost_sharing", "How costs and revenue are split", "contract_value")),
    ),
    CuadFamily(
        "co_branding", CONTRACT_SUBTYPE_LABELS["co_branding"],
        ("Co_Branding",),
        "Co-branding agreement: parties combine marks or properties on jointly branded products, sites or campaigns.",
        ("License Grant", "Non-Transferable License", "Ip Ownership Assignment", "Exclusivity",
         "Revenue/Profit Sharing", "Non-Compete", "Termination For Convenience",
         "Governing Law", "Audit Rights"),
        (_ff("co_branded_product", "Product, site or program carrying both brands"),
         _ff("licensed_marks", "Trademarks licensed to each party")),
    ),
    CuadFamily(
        "consulting", CONTRACT_SUBTYPE_LABELS["consulting"],
        ("Consulting Agreements",),
        "Consulting or advisory services agreement: a consultant provides expertise for fees.",
        ("Termination For Convenience", "Cap On Liability", "Ip Ownership Assignment",
         "Non-Compete", "No-Solicit Of Employees", "Insurance", "Expiration Date",
         "Governing Law", "Post-Termination Services"),
        (_ff("fee_structure", "Hourly rate, retainer or fixed fee", "contract_value"),
         _ff("services_scope", "Description of the consulting services")),
    ),
    CuadFamily(
        "development", CONTRACT_SUBTYPE_LABELS["development"],
        ("Development",),
        "Development agreement: one party builds a product, software or deliverable, often against milestones.",
        ("Ip Ownership Assignment", "Joint Ip Ownership", "License Grant", "Source Code Escrow",
         "Warranty Duration", "Termination For Convenience", "Post-Termination Services",
         "Expiration Date", "Cap On Liability", "Governing Law"),
        (_ff("deliverables", "Product or software to be developed"),
         _ff("milestone_fees", "Milestone or acceptance payments", "contract_value")),
    ),
    CuadFamily(
        "distributor", CONTRACT_SUBTYPE_LABELS["distributor"],
        ("Distributor",),
        "Distributor agreement: a distributor buys and resells the supplier's goods in a territory.",
        ("License Grant", "Exclusivity", "Minimum Commitment", "Anti-Assignment", "Non-Compete",
         "Price Restrictions", "Termination For Convenience", "Rofr/Rofo/Rofn",
         "Competitive Restriction Exception", "Volume Restriction", "Governing Law", "Renewal Term"),
        (_ff("territory", "Distribution territory"),
         _ff("distributed_products", "Products the distributor may sell"),
         _ff("minimum_purchase", "Minimum purchase or quota", "contract_value")),
    ),
    CuadFamily(
        "endorsement", CONTRACT_SUBTYPE_LABELS["endorsement"],
        ("Endorsement",),
        "Endorsement agreement (or rider): a person or entity lends name, likeness or reputation to promote a product.",
        ("License Grant", "Non-Transferable License", "Exclusivity", "Non-Disparagement",
         "Ip Ownership Assignment", "Expiration Date", "Termination For Convenience",
         "Non-Compete", "Governing Law"),
        (_ff("endorser", "Person or entity giving the endorsement"),
         _ff("endorsement_fee", "Endorsement fee or compensation", "contract_value")),
    ),
    CuadFamily(
        "franchise", CONTRACT_SUBTYPE_LABELS["franchise"],
        ("Franchise",),
        "Franchise agreement: a franchisor grants the right to operate under its system and marks in exchange for fees.",
        ("Non-Compete", "Exclusivity", "Revenue/Profit Sharing", "Audit Rights", "Minimum Commitment",
         "Renewal Term", "Cap On Liability", "License Grant", "Termination For Convenience",
         "Anti-Assignment", "Post-Termination Services", "Insurance", "No-Solicit Of Employees",
         "Rofr/Rofo/Rofn", "Governing Law"),
        (_ff("territory", "Franchised territory"),
         _ff("franchise_fees", "Initial franchise fee and royalty rate", "contract_value"),
         _ff("franchised_system", "Brand or business system franchised")),
    ),
    CuadFamily(
        "hosting", CONTRACT_SUBTYPE_LABELS["hosting"],
        ("Hosting",),
        "Hosting agreement: a provider hosts web sites, applications or data for a customer.",
        ("Price Restrictions", "Renewal Term", "Expiration Date", "Warranty Duration",
         "Cap On Liability", "Uncapped Liability", "Insurance", "Audit Rights",
         "Post-Termination Services", "Termination For Convenience", "Volume Restriction",
         "Source Code Escrow", "Governing Law"),
        (_ff("hosted_service", "Site, application or data being hosted"),
         _ff("service_levels", "Uptime or service-level commitments"),
         _ff("hosting_fees", "Recurring hosting fees", "contract_value")),
    ),
    CuadFamily(
        "ip", CONTRACT_SUBTYPE_LABELS["ip"],
        ("IP",),
        "Intellectual-property agreement: assignment, transfer or licensing of patents, marks, copyrights or software.",
        ("Ip Ownership Assignment", "License Grant", "Joint Ip Ownership", "Source Code Escrow",
         "Non-Transferable License", "Irrevocable Or Perpetual License", "Covenant Not To Sue",
         "Anti-Assignment", "Warranty Duration", "Governing Law"),
        (_ff("ip_assets", "Patents, marks, copyrights or software covered"),
         _ff("consideration", "Purchase price or royalty for the IP", "contract_value")),
    ),
    CuadFamily(
        "joint_venture", CONTRACT_SUBTYPE_LABELS["joint_venture"],
        ("Joint Venture", "Joint Venture _ Filing"),
        "Joint-venture agreement: parties form or operate a shared venture or project and divide contributions and returns.",
        ("Joint Ip Ownership", "Governing Law", "Change Of Control", "Revenue/Profit Sharing",
         "Non-Compete", "Exclusivity", "Rofr/Rofo/Rofn", "Anti-Assignment",
         "Termination For Convenience", "Ip Ownership Assignment", "Expiration Date",
         "Competitive Restriction Exception"),
        (_ff("venture_entity", "Name of the joint venture entity or project"),
         _ff("capital_contributions", "Each party's contribution", "contract_value"),
         _ff("ownership_split", "Ownership or profit-split percentages")),
    ),
    CuadFamily(
        "license", CONTRACT_SUBTYPE_LABELS["license"],
        ("License_Agreements",),
        "License agreement: one party licenses technology, content or IP to another, typically for royalties.",
        ("License Grant", "Non-Transferable License", "Irrevocable Or Perpetual License",
         "Ip Ownership Assignment", "Affiliate License-Licensor", "Affiliate License-Licensee",
         "Source Code Escrow", "Unlimited/All-You-Can-Eat-License", "Revenue/Profit Sharing",
         "Exclusivity", "Covenant Not To Sue", "Audit Rights", "Anti-Assignment",
         "Termination For Convenience", "Joint Ip Ownership", "Most Favored Nation"),
        (_ff("royalty_rate", "Royalty rate or license fee", "contract_value"),
         _ff("territory", "Licensed territory"),
         _ff("licensed_product", "Licensed technology, content or product"),
         _ff("license_scope", "Exclusive / non-exclusive, field of use, sublicensing")),
    ),
    CuadFamily(
        "maintenance", CONTRACT_SUBTYPE_LABELS["maintenance"],
        ("Maintenance",),
        "Maintenance or support agreement: a provider keeps software, equipment or systems operating.",
        ("Warranty Duration", "Post-Termination Services", "Expiration Date", "Renewal Term",
         "Price Restrictions", "Cap On Liability", "Termination For Convenience",
         "Source Code Escrow", "Insurance", "Notice Period To Terminate Renewal"),
        (_ff("supported_product", "Software or equipment under maintenance"),
         _ff("support_fees", "Annual or recurring maintenance fee", "contract_value")),
    ),
    CuadFamily(
        "manufacturing", CONTRACT_SUBTYPE_LABELS["manufacturing"],
        ("Manufacturing",),
        "Manufacturing agreement: one party makes goods to specification for another, often with supply commitments.",
        ("Minimum Commitment", "Volume Restriction", "Price Restrictions", "Warranty Duration",
         "Exclusivity", "Insurance", "Ip Ownership Assignment", "License Grant",
         "Most Favored Nation", "Termination For Convenience", "Audit Rights", "Uncapped Liability"),
        (_ff("manufactured_product", "Goods to be manufactured"),
         _ff("minimum_volume", "Minimum or forecast volumes"),
         _ff("unit_price", "Unit price or pricing formula", "contract_value")),
    ),
    CuadFamily(
        "marketing", CONTRACT_SUBTYPE_LABELS["marketing"],
        ("Marketing",),
        "Marketing agreement: a party markets, promotes or co-promotes products or services.",
        ("Exclusivity", "Non-Compete", "Revenue/Profit Sharing", "License Grant",
         "Termination For Convenience", "Expiration Date", "Renewal Term",
         "Ip Ownership Assignment", "Minimum Commitment", "Competitive Restriction Exception",
         "Governing Law"),
        (_ff("marketed_product", "Product or service being marketed"),
         _ff("marketing_fees", "Fees or commission for marketing services", "contract_value")),
    ),
    CuadFamily(
        "non_compete_no_solicit", CONTRACT_SUBTYPE_LABELS["non_compete_no_solicit"],
        ("Non_Compete_Non_Solicit",),
        "Restrictive-covenant agreement: non-compete, non-solicit and non-disparagement undertakings.",
        ("Non-Compete", "No-Solicit Of Customers", "No-Solicit Of Employees", "Non-Disparagement",
         "Competitive Restriction Exception", "Expiration Date", "Governing Law",
         "Covenant Not To Sue", "Liquidated Damages", "Exclusivity"),
        (_ff("restricted_period", "Duration of the restrictive covenants", "term_length"),
         _ff("restricted_territory", "Geography the restriction covers"),
         _ff("restricted_business", "Business or activity restricted")),
    ),
    CuadFamily(
        "outsourcing", CONTRACT_SUBTYPE_LABELS["outsourcing"],
        ("Outsourcing",),
        "Outsourcing agreement: a provider takes over a business process or function for a customer.",
        ("Audit Rights", "Cap On Liability", "Uncapped Liability", "Termination For Convenience",
         "Post-Termination Services", "Change Of Control", "Insurance", "Minimum Commitment",
         "Volume Restriction", "Price Restrictions", "Expiration Date", "No-Solicit Of Employees",
         "Liquidated Damages", "Ip Ownership Assignment"),
        (_ff("outsourced_function", "Business process or function outsourced"),
         _ff("service_levels", "Service levels and credits"),
         _ff("service_fees", "Base fee or charges", "contract_value")),
    ),
    CuadFamily(
        "promotion", CONTRACT_SUBTYPE_LABELS["promotion"],
        ("Promotion",),
        "Promotion agreement: promotional services, campaigns or co-promotion of a product.",
        ("Exclusivity", "Revenue/Profit Sharing", "Expiration Date", "Renewal Term",
         "License Grant", "Non-Disparagement", "Termination For Convenience",
         "Minimum Commitment", "Non-Compete", "Ip Ownership Assignment"),
        (_ff("promoted_product", "Product or service being promoted"),
         _ff("promotion_fees", "Fees or compensation for promotion", "contract_value")),
    ),
    CuadFamily(
        "reseller", CONTRACT_SUBTYPE_LABELS["reseller"],
        ("Reseller",),
        "Reseller agreement: a reseller or value-added reseller buys or licenses products to resell to customers.",
        ("License Grant", "Minimum Commitment", "Anti-Assignment", "Price Restrictions",
         "Exclusivity", "Non-Transferable License", "Most Favored Nation",
         "Termination For Convenience", "Volume Restriction", "Rofr/Rofo/Rofn",
         "Competitive Restriction Exception", "Audit Rights", "Warranty Duration"),
        (_ff("territory", "Reseller territory"),
         _ff("resold_products", "Products the reseller may resell"),
         _ff("reseller_discount", "Discount off list price", "contract_value")),
    ),
    CuadFamily(
        "service", CONTRACT_SUBTYPE_LABELS["service"],
        ("Service",),
        "Service agreement: general professional, support or operational services for fees.",
        ("Termination For Convenience", "Warranty Duration", "Cap On Liability", "Expiration Date",
         "Renewal Term", "Insurance", "Ip Ownership Assignment", "Post-Termination Services",
         "Uncapped Liability", "Audit Rights", "Governing Law", "No-Solicit Of Employees",
         "Price Restrictions"),
        (_ff("services_scope", "Description of the services"),
         _ff("service_fees", "Service fees or rate card", "contract_value")),
    ),
    CuadFamily(
        "sponsorship", CONTRACT_SUBTYPE_LABELS["sponsorship"],
        ("Sponsorship",),
        "Sponsorship agreement: a sponsor funds an event, team or content in exchange for promotional benefits.",
        ("Exclusivity", "License Grant", "Non-Transferable License", "Revenue/Profit Sharing",
         "Rofr/Rofo/Rofn", "Termination For Convenience", "Expiration Date", "Renewal Term",
         "Competitive Restriction Exception", "Non-Disparagement", "Ip Ownership Assignment",
         "Minimum Commitment"),
        (_ff("sponsored_property", "Event, team or content sponsored"),
         _ff("sponsorship_fee", "Sponsorship fee", "contract_value"),
         _ff("sponsor_benefits", "Rights and benefits the sponsor receives")),
    ),
    CuadFamily(
        "strategic_alliance", CONTRACT_SUBTYPE_LABELS["strategic_alliance"],
        ("Strategic Alliance",),
        "Strategic-alliance agreement: parties partner on a commercial initiative without forming a separate entity.",
        ("Joint Ip Ownership", "Exclusivity", "Change Of Control", "Non-Compete",
         "Revenue/Profit Sharing", "Rofr/Rofo/Rofn", "Competitive Restriction Exception",
         "License Grant", "Ip Ownership Assignment", "Termination For Convenience",
         "Governing Law", "No-Solicit Of Customers", "No-Solicit Of Employees", "Anti-Assignment"),
        (_ff("alliance_scope", "Field or initiative of the alliance"),
         _ff("revenue_split", "How revenue or profit is divided")),
    ),
    CuadFamily(
        "supply", CONTRACT_SUBTYPE_LABELS["supply"],
        ("Supply",),
        "Supply agreement: a supplier sells goods or materials to a buyer on stated quantities and prices.",
        ("Minimum Commitment", "Volume Restriction", "Price Restrictions", "Governing Law",
         "Warranty Duration", "Exclusivity", "Most Favored Nation", "Insurance",
         "Termination For Convenience", "Uncapped Liability", "Cap On Liability",
         "Expiration Date", "Renewal Term", "Audit Rights"),
        (_ff("supplied_goods", "Goods or materials supplied"),
         _ff("minimum_quantity", "Minimum or forecast quantities"),
         _ff("unit_price", "Unit price or pricing formula", "contract_value")),
    ),
    CuadFamily(
        "transportation", CONTRACT_SUBTYPE_LABELS["transportation"],
        ("Transportation",),
        "Transportation agreement: carriage, logistics or freight services.",
        ("Insurance", "Cap On Liability", "Uncapped Liability", "Price Restrictions",
         "Volume Restriction", "Minimum Commitment", "Termination For Convenience",
         "Expiration Date", "Exclusivity", "Audit Rights", "Liquidated Damages",
         "Anti-Assignment", "Governing Law"),
        (_ff("route_or_lane", "Routes, lanes or territory served"),
         _ff("freight_rates", "Freight rates or fees", "contract_value")),
    ),
    CuadFamily(
        SUBTYPE_UNKNOWN, "Other / unlisted",
        (),
        "Commercial contract that fits none of the 25 listed families; extract every present category from the text and never invent a family from the filename.",
        (),
    ),
)

#: Family keys in catalog order (25 families + ``other``).
CUAD_FAMILY_KEYS: tuple[str, ...] = tuple(f.key for f in CUAD_FAMILIES)


# ---------------------------------------------------------------------------
# ContractExtraction field descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CuadField:
    """One scored ContractExtraction field.

    ``empty_kind`` is ``"null"`` (unstated scalar -> ``None``) or ``"list"``
    (unstated list -> ``[]``); use :attr:`empty_value` for a fresh value.
    """

    name: str
    scoring_type: str
    empty_kind: str
    description: str

    @property
    def empty_value(self) -> Any:
        """Fresh empty value: ``None`` for scalars, a new ``[]`` for lists."""
        return [] if self.empty_kind == "list" else None

    @property
    def is_list(self) -> bool:
        """Return whether the field uses a list-valued empty representation."""
        return self.empty_kind == "list"


CONTRACT_EXTRACTION_FIELDS: tuple[CuadField, ...] = (
    CuadField(
        "document_name", "name", "null",
        "The contract's name as given (e.g. 'Web Hosting Agreement'); never invented from the file name.",
    ),
    CuadField(
        "parties", "entity_list:name", "list",
        "Distinct named contracting parties: full legal name plus parenthetical alias when the text gives one.",
    ),
    CuadField(
        "effective_date", "date", "null",
        "Date the agreement takes effect as ISO YYYY-MM-DD; a defined Effective Date wins over the signature date.",
    ),
    CuadField(
        "term_length", "free_text", "null",
        "Duration of the agreement itself (commencement through end / earlier termination), not a sub-period.",
    ),
    CuadField(
        "governing_law", "name", "null",
        "Only the sentence naming the jurisdiction whose laws govern; no forum, venue or attorneys' fees.",
    ),
    CuadField(
        "contract_value", "money", "null",
        "Monetary consideration as a plain currency phrase ('$2,000,000'); $0 is a stated amount; never computed.",
    ),
    CuadField(
        "renewal_terms", "free_text", "null",
        "Renewal, extension or rollover language, including evergreen 'continues until terminated on N days notice'.",
    ),
    CuadField(
        "cuad_family", "name", "null",
        "Exactly one CUAD family key (25 families) or 'other'; null only when not a CUAD commercial contract.",
    ),
    CuadField(
        "merger_consideration", "name", "null",
        "Parse-compat only: always null on CUAD commercial contracts.",
    ),
    CuadField(
        "cuad_clauses", "entity_list:free_text", "list",
        "Present CUAD categories only, as '<Atticus category>: <short verbatim evidence span>'.",
    ),
    CuadField(
        "maud_clauses", "entity_list:free_text", "list",
        "Parse-compat only: always [] on CUAD commercial contracts.",
    ),
)

#: ``{field: scoring type}`` view of :data:`CONTRACT_EXTRACTION_FIELDS`.
CONTRACT_FIELD_TYPES: dict[str, str] = {
    f.name: f.scoring_type for f in CONTRACT_EXTRACTION_FIELDS
}


# ---------------------------------------------------------------------------
# Import-time invariants
# ---------------------------------------------------------------------------

def _check_invariants() -> None:
    """Fail loudly if the catalog drifts from the corpus / scoring authority."""
    if set(CUAD_CATEGORY_NAMES) != set(CUAD_CLAUSE_CATEGORIES) or len(
        CUAD_CATEGORY_NAMES
    ) != len(set(CUAD_CATEGORY_NAMES)):
        raise RuntimeError("cuad_schema categories drifted from corpus.CUAD_CLAUSE_CATEGORIES")
    for cat in CUAD_CATEGORIES:
        if cat.group not in CUAD_GROUPS or cat.answer_format not in CUAD_ANSWER_FORMATS:
            raise RuntimeError(f"cuad_schema: bad group/format on {cat.name!r}")
        if cat.field is not None and cat.field not in CONTRACT_FIELD_TYPES:
            raise RuntimeError(f"cuad_schema: {cat.name!r} maps to unknown field {cat.field!r}")
    expected_keys = set(DOC_TYPE_SUBCLASSES["contract"]) | {SUBTYPE_UNKNOWN}
    if set(CUAD_FAMILY_KEYS) != expected_keys or len(CUAD_FAMILY_KEYS) != 26:
        raise RuntimeError("cuad_schema families drifted from corpus.DOC_TYPE_SUBCLASSES['contract']")
    surfaces = {s for f in CUAD_FAMILIES for s in f.surfaces}
    if surfaces != set(CORPUS_SUBCLASS_SURFACES["contract"]):
        raise RuntimeError("cuad_schema family surfaces drifted from corpus.CORPUS_SUBCLASS_SURFACES")
    for fam in CUAD_FAMILIES:
        stray = set(fam.salient) - set(CUAD_CATEGORY_NAMES)
        if stray or len(set(fam.salient)) != len(fam.salient):
            raise RuntimeError(f"cuad_schema: bad salient set on {fam.key!r}: {sorted(stray)}")
        for hint in fam.family_fields:
            if hint.informs is not None and hint.informs not in CONTRACT_FIELD_TYPES:
                raise RuntimeError(f"cuad_schema: {fam.key}.{hint.name} informs unknown field")
    if CONTRACT_FIELD_TYPES != DEFAULT_FIELD_TYPES["contract"]:
        raise RuntimeError("cuad_schema field descriptor drifted from DEFAULT_FIELD_TYPES['contract']")


_check_invariants()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"[^a-z0-9]")


def _key(value: Any) -> str:
    """Build a case-insensitive lookup key by removing configured separators."""
    return _KEY_RE.sub("", str(value).strip().lower())


#: Common non-canonical spellings -> canonical Atticus name. Punctuation,
#: spacing and case are already ignored by the lookup, so only genuinely
#: different words are listed (``ROFR/ROFO/ROFN`` and ``Non-Compete`` /
#: ``Noncompete`` need no entry).
_CATEGORY_ALIASES: dict[str, str] = {
    "Rofr/Rofo/Rofn": "Rofr/Rofo/Rofn",
    "ROFR": "Rofr/Rofo/Rofn",
    "ROFO": "Rofr/Rofo/Rofn",
    "ROFN": "Rofr/Rofo/Rofn",
    "Right of First Refusal": "Rofr/Rofo/Rofn",
    "Right of First Offer": "Rofr/Rofo/Rofn",
    "Right of First Negotiation": "Rofr/Rofo/Rofn",
    "MFN": "Most Favored Nation",
    "Most Favoured Nation": "Most Favored Nation",
    "Covenant Not To Compete": "Non-Compete",
    "Non-Solicit Of Customers": "No-Solicit Of Customers",
    "Non-Solicitation Of Customers": "No-Solicit Of Customers",
    "Non-Solicit Of Employees": "No-Solicit Of Employees",
    "Non-Solicitation Of Employees": "No-Solicit Of Employees",
    "No-Hire": "No-Solicit Of Employees",
    "Termination Without Cause": "Termination For Convenience",
    "Assignment": "Anti-Assignment",
    "Profit Sharing": "Revenue/Profit Sharing",
    "Revenue Sharing": "Revenue/Profit Sharing",
    "Minimum Purchase": "Minimum Commitment",
    "IP Assignment": "Ip Ownership Assignment",
    "Joint IP": "Joint Ip Ownership",
    "Enterprise License": "Unlimited/All-You-Can-Eat-License",
    "Unlimited License": "Unlimited/All-You-Can-Eat-License",
    "All You Can Eat License": "Unlimited/All-You-Can-Eat-License",
    "Perpetual License": "Irrevocable Or Perpetual License",
    "Irrevocable License": "Irrevocable Or Perpetual License",
    "Escrow": "Source Code Escrow",
    "Limitation Of Liability": "Cap On Liability",
    "Liability Cap": "Cap On Liability",
    "Warranty Period": "Warranty Duration",
    "Notice Period": "Notice Period To Terminate Renewal",
    "Non-Renewal Notice": "Notice Period To Terminate Renewal",
    "Expiry Date": "Expiration Date",
    "Choice Of Law": "Governing Law",
    "Contract Name": "Document Name",
    "Name Of Contract": "Document Name",
}

_CATEGORY_INDEX: dict[str, CuadCategory] = {_key(c.name): c for c in CUAD_CATEGORIES}
for _alias, _canonical in _CATEGORY_ALIASES.items():
    if _canonical not in CUAD_CATEGORY_NAMES:
        raise RuntimeError(f"cuad_schema: alias {_alias!r} -> unknown {_canonical!r}")
    _existing = _CATEGORY_INDEX.get(_key(_alias))
    if _existing is not None and _existing.name != _canonical:
        raise RuntimeError(f"cuad_schema: alias {_alias!r} collides with {_existing.name!r}")
    _CATEGORY_INDEX[_key(_alias)] = _CATEGORY_INDEX[_key(_canonical)]
del _alias, _canonical, _existing

_FAMILY_INDEX: dict[str, CuadFamily] = {f.key: f for f in CUAD_FAMILIES}


def canonical_cuad_category(value: Any) -> str | None:
    """Canonical Atticus name for ``value`` (case, whitespace, punctuation
    and common aliases ignored), or ``None`` when it names no category."""
    if value is None:
        return None
    hit = _CATEGORY_INDEX.get(_key(value))
    return hit.name if hit else None


def cuad_category(name: Any) -> CuadCategory:
    """Look up a category by name or alias.

    ``"ROFR/ROFO/ROFN"``, ``"rofr rofo rofn"``, ``"Noncompete"`` and
    ``"IP Ownership Assignment"`` all resolve. Unknown names raise
    ``KeyError`` — never a silent fallback.
    """
    hit = _CATEGORY_INDEX.get(_key(name)) if name is not None else None
    if hit is None:
        raise KeyError(
            f"unknown CUAD category {name!r}; expected one of {list(CUAD_CATEGORY_NAMES)}"
        )
    return hit


def cuad_family(name: Any = None) -> CuadFamily:
    """Look up a family by key, label or CUAD folder surface.

    Resolution reuses :func:`llm_dojo_scoring.equivalences.normalize_subtype`
    (``"License_Agreements"``, ``"Joint Venture _ Filing"``, ``"License
    Agreement"`` ...). Unknown, empty or non-contract values resolve to the
    ``other`` family, matching the sorter's unknown bucket.
    """
    if isinstance(name, CuadFamily):
        return name
    raw = str(name).strip() if name is not None else ""
    if raw in _FAMILY_INDEX:
        return _FAMILY_INDEX[raw]
    return _FAMILY_INDEX.get(normalize_subtype(name), _FAMILY_INDEX["other"])


def salient_categories(family: Any = None) -> tuple[str, ...]:
    """Salient category names for ``family`` in priority order (``()`` for
    ``None`` / ``other``)."""
    return () if family is None else cuad_family(family).salient


# ---------------------------------------------------------------------------
# Questions and prompt block
# ---------------------------------------------------------------------------

def _ordered(family: Any) -> list[CuadCategory]:
    """Categories with the family's salient ones first, rest in CUAD order."""
    salient = [cuad_category(n) for n in salient_categories(family)]
    seen = {c.name for c in salient}
    return salient + [c for c in CUAD_CATEGORIES if c.name not in seen]


def _question(cat: CuadCategory, style: str) -> str:
    """Select original CUAD or specialist wording, rejecting unknown styles."""
    if style == "cuad":
        return cat.question
    if style == "specialist":
        return cat.prompt_question
    raise ValueError(f"unknown question style {style!r}; expected 'cuad' or 'specialist'")


def cuad_questions(
    family: Any = None, *, style: str = "cuad", salient_only: bool = False
) -> list[tuple[str, str]]:
    """Ordered ``[(category, question), ...]`` for the 41 categories.

    With a ``family``, its salient categories come first in priority order and
    the rest follow in CUAD order; ``salient_only`` drops the rest. ``style``
    is ``"cuad"`` (the CUAD lawyer-review wording) or ``"specialist"`` (the
    concise prompt question). Without a family the order is CUAD order.
    """
    cats = _ordered(family)
    if salient_only:
        keep = set(salient_categories(family))
        cats = [c for c in cats if c.name in keep]
    return [(c.name, _question(c, style)) for c in cats]


def render_cuad_question_block(
    family: Any = None, *, style: str = "specialist", categories: Iterable[str] | None = None
) -> str:
    """Prompt-ready text block of the CUAD clause questions.

    Salient categories for ``family`` are listed first under their own
    heading. A category that also feeds a scored field is tagged
    ``[also <field>]``. ``categories`` restricts the block to a subset (any
    accepted alias). The block only restates the question set; the output
    contract (``'<Category>: <verbatim span>'``, present categories only)
    stays with the frozen specialist prompt.
    """
    wanted = None if categories is None else {cuad_category(c).name for c in categories}

    def line(cat: CuadCategory) -> str:
        """Render a category question with its optional scored-field tag."""
        tag = f" [also {cat.field}]" if cat.field else ""
        return f"- {cat.name}{tag}: {_question(cat, style)}"

    cats = [c for c in _ordered(family) if wanted is None or c.name in wanted]
    salient = set(salient_categories(family))
    head = [c for c in cats if c.name in salient]
    tail = [c for c in cats if c.name not in salient]
    out = [
        "CUAD clause questions. Emit only categories whose answer is present, "
        "as '<Category>: <short verbatim span>' using the exact category name."
    ]
    if family is not None:
        fam = cuad_family(family)
        out.append(f"Contract family: {fam.key} ({fam.label}). {fam.definition}")
    if head:
        out.append(f"Salient for {cuad_family(family).key} (check these first):")
        out.extend(line(c) for c in head)
    if tail:
        out.append("Other categories:" if head else "Categories:")
        out.extend(line(c) for c in tail)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------

_REGEX_SPECIAL = re.compile(r"([.^$*+?()\[\]{}|\\])")


def _clause_pattern() -> str:
    """``^(?:Cat A|Cat B|...): .+`` over the 41 exact Atticus names."""
    alts = "|".join(_REGEX_SPECIAL.sub(r"\\\1", n) for n in CUAD_CATEGORY_NAMES)
    return f"^(?:{alts}): .+"


def cuad_json_schema(*, include_trace: bool = True) -> dict[str, Any]:
    """JSON Schema (draft-07-compatible subset) for ``ContractExtraction``.

    Built for vLLM guided decoding: nullable scalars are
    ``["string", "null"]``, lists default to ``[]``, ``cuad_family`` is the 25
    families + ``other`` (or ``null``), and each ``cuad_clauses`` item must
    match ``^<Atticus category>: .+``. ``merger_consideration`` is pinned to
    ``null`` and ``maud_clauses`` to ``[]`` (parse-compat keys, always empty on
    CUAD contracts). Every scored field is required. ``include_trace`` adds the
    prompt's unscored ``reasoning`` object and ``confidence`` number; set it
    ``False`` for a schema over exactly the 11 scored keys.
    """
    string_or_null: dict[str, Any] = {"type": ["string", "null"], "default": None}
    props: dict[str, Any] = {
        "document_name": {**string_or_null, "description": CONTRACT_EXTRACTION_FIELDS[0].description},
        "parties": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "default": [],
            "description": CONTRACT_EXTRACTION_FIELDS[1].description,
        },
        "effective_date": {
            "type": ["string", "null"],
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "default": None,
            "description": CONTRACT_EXTRACTION_FIELDS[2].description,
        },
        "term_length": {**string_or_null, "description": CONTRACT_EXTRACTION_FIELDS[3].description},
        "governing_law": {**string_or_null, "description": CONTRACT_EXTRACTION_FIELDS[4].description},
        "contract_value": {**string_or_null, "description": CONTRACT_EXTRACTION_FIELDS[5].description},
        "renewal_terms": {**string_or_null, "description": CONTRACT_EXTRACTION_FIELDS[6].description},
        "cuad_family": {
            "type": ["string", "null"],
            "enum": [*CUAD_FAMILY_KEYS, None],
            "default": None,
            "description": CONTRACT_EXTRACTION_FIELDS[7].description,
        },
        "merger_consideration": {
            "type": "null",
            "default": None,
            "description": CONTRACT_EXTRACTION_FIELDS[8].description,
        },
        "cuad_clauses": {
            "type": "array",
            "items": {"type": "string", "pattern": _clause_pattern()},
            "default": [],
            "description": CONTRACT_EXTRACTION_FIELDS[9].description,
        },
        "maud_clauses": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 0,
            "default": [],
            "description": CONTRACT_EXTRACTION_FIELDS[10].description,
        },
    }
    if props.keys() != CONTRACT_FIELD_TYPES.keys():  # pragma: no cover - guard
        raise RuntimeError("cuad_json_schema props drifted from the field descriptor")
    required = [f.name for f in CONTRACT_EXTRACTION_FIELDS]
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
                        "required": ["field", "evidence"],
                    },
                    "default": [],
                },
            },
            "required": ["summary", "entries"],
            "description": "Trace only, produced before the values; never scored.",
        }
        props["confidence"] = {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Evidence-based 0.0-1.0 confidence; never scored.",
        }
        required = ["reasoning", *required, "confidence"]
    return {
        "title": "ContractExtraction",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }
