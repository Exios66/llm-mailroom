"""Docclass prompt variants for every mailroom classification-chain role.

KANBAN-090 (2026-08-23, human directive via Discord #hermes): mirrors the
llm-entity-extraction docclass prompt arm (``src/prompts_docclass.py`` there).
Every variant here is DERIVED from this repo's own production base — the
exact string in ``llm.prompts.prompt_templates()`` — by PURE APPENDITION.
``variant.startswith(base)`` holds in full, the base bytes are untouched, and
the docclass block rides after the production closer as additive context.
Nothing is replaced, so no production anchor can drift.

    role                          production agent_name           key in DOCCLASS_PROMPT_VERSIONS
    -----------------------------  -----------------------------  --------------------------------------
    sorter                         sorter                         sorter_docclass_v0
    contracts_specialist           contracts_specialist           contracts_specialist_docclass_v0
    corporate_records_specialist   corporate_records_specialist   corporate_records_specialist_docclass_v0
    correspondence_specialist      correspondence_specialist      correspondence_specialist_docclass_v0
    compliance_specialist          compliance_specialist          compliance_specialist_docclass_v0
    insurance_claims_specialist    insurance_claims_specialist    insurance_claims_specialist_docclass_v0
    reviewer (second opinion)      sorter_reviewer                reviewer_docclass_v0
    arbiter                        arbiter                        arbiter_docclass_v0
    boss                           boss                           boss_docclass_v0
    judge (completeness)           judge                          judge_docclass_v0
    judge (classification)         judge-classification           judge_classification_docclass_v0
    judge (correctness)            judge-correctness              judge_correctness_docclass_v0

DEPLOYMENT: mailroom's Langfuse production surface is the agent-name-pinned
templates in ``prompt_templates()`` (`mailroom-<agent_name>`). Docclass
variants must NEVER flow through prompt_templates(): that would overwrite
production agent prompts. They ship as this standalone registry and reach
Langfuse only via the OPT-IN sync path::

    python scripts/sync_prompts.py --docclass

which pushes them under distinct ``mailroom-docclass-<key>`` names,
content-keyed and idempotent like every other sync. Runtime routes are
untouched: no pipeline fetches a docclass key by default.
"""

from __future__ import annotations

# =============================================================================
# Shared docclass context block — kept byte-compatible with the entity repo's
# module so both arms speak identical class-set language.
#
# NOTE: fragment assertions in tests target SHORT substrings that do not cross
# a source line boundary (rendered \n between segments).
# =============================================================================
_DOCCONTEXT = (
    "DOCCLASS ARM CONTEXT (hierarchical document-classification mode): the "
    "document you receive was classified by the docclass sorter over the "
    "EXTENDED primary class set — contract, corporate_record, due_diligence, "
    "correspondence, compliance_filing, court_opinion, insurance_claim, "
    "merger_agreement — with a second-level doc_subclass where the class has "
    "one: contract -> contract_subtype (the CUAD-style subtype taxonomy); "
    "merger_agreement -> consideration type (all_cash, all_stock, "
    "mixed_cash_stock, mixed_cash_stock_election, other); corporate_record -> "
    "record type read from the document's own title/head (bylaws, "
    "articles_of_incorporation, certificate_of_formation, charter_amendment, "
    "powers_of_attorney, subsidiary_list, rights_instrument, indenture, "
    "board_resolution, officer_certificate, other).\n"
)


def _rules(body: str) -> str:
    return "DOCLASS RULES FOR THIS ROLE:\n" + body


_SPECIALIST_RULES_BODY = (
    "a. The assigned doc_type/doc_subclass is pipeline ROUTING STATE, not "
    "ground truth: verify it against the visible text before relying on it, "
    "and ground every extracted field in the document as it actually reads.\n"
    "b. If the substantive form clearly contradicts the assignment, extract "
    "your schema fields from the document AS IT IS — do not force another "
    "class's fields onto it; rerouting is the classification chain's job.\n"
    "c. Claim-documentation leakage: FNOL forms, adjuster reports/estimates, "
    "demand packages, coverage determinations, reservation-of-rights and "
    "denial letters may arrive under contract or correspondence labels — read "
    "visible claim facts as claim facts regardless of label.\n"
    "d. M&A leakage: merger_agreement documents may carry contract labels — "
    "treat Parent/Merger Sub machinery, Effective Time/Closing mechanics, and "
    "Exchange Ratio/Merger Consideration language as ordinary extraction "
    "evidence wherever it appears.\n"
    "The output-format requirements of the prompt above are unchanged: return "
    "exactly one JSON object matching the schema and no other text."
)

_CONTRACTS_SPECIALIST_RULES_BODY = (
    _SPECIALIST_RULES_BODY.rsplit(
        "The output-format requirements of the prompt above are unchanged:", 1
    )[0]
    +     "e. CUAD families: when the sorter subtype is one of the 25 CUAD "
    "agreement families (affiliate, agency, collaboration, co_branding, "
    "consulting, development, distributor, endorsement, franchise, hosting, "
    "ip, joint_venture, license, maintenance, manufacturing, marketing, "
    "non_compete_no_solicit, outsourcing, promotion, reseller, service, "
    "sponsorship, strategic_alliance, supply, transportation), extract THAT "
    "family's characteristic operative clauses verbatim into key_obligations "
    "and termination_clauses. Do not substitute a paraphrase or a different "
    "family's clause set. Joint Filing Agreements (Exchange Act 13(d)/13(g)) "
    "are the joint_venture family. Set cuad_family to that family key.\n"
    "f. MAUD mergers: when doc_type is merger_agreement (or the text is an "
    "Agreement and Plan of Merger), set merger_consideration AND contract_value "
    "to exactly one consideration token — all_cash, all_stock, mixed_cash_stock, "
    "mixed_cash_stock_election, or other — matching the Merger Consideration "
    "/ Conversion of Shares mechanics. Put the surviving corporation, "
    "exchange ratio, and Effective Time into key_obligations as verbatim "
    "operative language. cuad_family is null.\n"
    "g. CUAD clause content: emit every PRESENT Atticus category in "
    "cuad_clauses as '<Category>: <verbatim span>' using the exact names "
    "Document Name, Parties, Agreement Date, Effective Date, Expiration Date, "
    "Renewal Term, Notice Period To Terminate Renewal, Governing Law, Most "
    "Favored Nation, Competitive Restriction Exception, Non-Compete, "
    "Exclusivity, No-Solicit Of Customers, No-Solicit Of Employees, "
    "Non-Disparagement, Termination For Convenience, Rofr/Rofo/Rofn, Change "
    "Of Control, Anti-Assignment, Revenue/Profit Sharing, Price Restrictions, "
    "Minimum Commitment, Volume Restriction, Ip Ownership Assignment, Joint "
    "Ip Ownership, License Grant, Non-Transferable License, Affiliate "
    "License-Licensor, Affiliate License-Licensee, Unlimited/All-You-Can-Eat-"
    "License, Irrevocable Or Perpetual License, Source Code Escrow, "
    "Post-Termination Services, Audit Rights, Uncapped Liability, Cap On "
    "Liability, Liquidated Damages, Warranty Duration, Insurance, Covenant "
    "Not To Sue, Third Party Beneficiary. Omit categories the visible text "
    "does not contain.\n"
    "h. MAUD clause content: emit every answered MAUD question in maud_clauses "
    "as '<Question>: <Answer>' using the exact question names Absence of "
    "Litigation Closing Condition, Accuracy of Target R&W Closing Condition, "
    "Agreement provides for matching rights in connection with COR, Agreement "
    "provides for matching rights in connection with FTR, Breach of Meeting "
    "Covenant, Breach of No Shop, Compliance with Covenant Closing Condition, "
    "FTR Triggers, Fiduciary exception to COR covenant, Fiduciary exception:  "
    "Board determination (no-shop), General Antitrust Efforts Standard, "
    "Intervening Event Definition, Knowledge Definition, Limitations on FTR "
    "Exercise, MAE Definition, Negative interim operating covenant, No-Shop, "
    "Ordinary course covenant, Specific Performance, Superior Offer "
    "Definition, Tail Period & Acquisition Proposal Details, Type of "
    "Consideration. The Answer must be the Hub valid_class string (Yes/No, "
    "All Cash, All Stock, Mixed Cash/Stock, Mixed Cash/Stock: Election, "
    "Continuous matching right, General R&Ws, …), not a paraphrase. Omit "
    "unanswered questions. Empty maud_clauses when the document is not a "
    "merger agreement.\n"
    "The output-format requirements of the prompt above are unchanged: return "
    "exactly one JSON object matching the schema and no other text."
)

_SORTER_RULES_BODY = (
    "a. Classify against the EXTENDED primary set — contract, "
    "corporate_record, due_diligence, correspondence, compliance_filing, "
    "court_opinion, insurance_claim, merger_agreement — plus unknown when "
    "none fit. Never remap an unknown onto correspondence.\n"
    "b. Family discriminators: acquisition machinery (Parent/Merger Sub, "
    "Effective Time, Exchange Ratio) makes a document merger_agreement, not "
    "contract; claim documentation (FNOL, adjuster reports, demand packages, "
    "coverage determinations, denial letters) is insurance_claim; records "
    "EMBEDDED as exhibits inside a parent agreement never change the parent's "
    "class.\n"
    "c. When doc_type is contract, contract_subtype MUST be one of the 25 "
    "CUAD families (or other). Do not invent a family. Joint Filing "
    "Agreements are joint_venture. License-and-maintenance hybrids follow "
    "the CUAD folder convention (maintenance).\n"
    "d. When doc_type is merger_agreement, contract_subtype is null — MAUD "
    "consideration type is an extraction field, not a CUAD family.\n"
    "The output-format requirements of the prompt above are unchanged."
)

_JUDGE_RULES_BODY = (
    "a. Completeness and correctness are judged WITHIN the registered schema "
    "for the document's class — never demand fields that belong to another "
    "class's schema.\n"
    "b. Cross-family leakage check: when populated values systematically "
    "describe a different document form than the class implies (claim facts "
    "inside a contract extraction), say so explicitly and lower confidence in "
    "the affected fields rather than failing the extraction wholesale.\n"
    "c. Verify against the visible source only (unchanged doctrine); when "
    "subclass-shaped fields appear, require quoted support for the SPECIFIC "
    "subclass, not merely the primary class.\n"
    "The output-format requirements of the prompt above are unchanged."
)

_JUDGE_CLASSIFICATION_RULES_BODY = (
    "a. You are grading the classification chain itself: judge doc_type AND "
    "doc_subclass against the EXTENDED primary set — contract, "
    "corporate_record, due_diligence, correspondence, compliance_filing, "
    "court_opinion, insurance_claim, merger_agreement.\n"
    "b. Family discriminators: acquisition machinery (Parent/Merger Sub, "
    "Effective Time, Exchange Ratio) makes a document merger_agreement, not "
    "contract; claim documentation (FNOL, adjuster reports, demand packages, "
    "coverage determinations, denial letters) is insurance_claim; records "
    "EMBEDDED as exhibits inside a parent agreement never change the parent's "
    "class.\n"
    "c. expected_class must be an exact key from the extended list; leave it "
    "null when the assigned class is correct.\n"
    "The output-format requirements of the prompt above are unchanged."
)

_BOSS_RULES_BODY = (
    "a. A conflict that traces to a CLASSIFICATION fault (both extractions "
    "internally consistent but describing materially different document "
    "forms) cannot be fixed by a merge: prefer human review and name the "
    "suspected upstream misclassification.\n"
    "b. The extended class set includes insurance_claim and merger_agreement; "
    "when deciding which specialist's output reflects the document's real "
    "form, weigh the family discriminators (acquisition machinery -> "
    "merger_agreement; FNOL/adjuster/coverage-denial material -> "
    "insurance_claim).\n"
    "The output-format requirements of the prompt above are unchanged."
)

_REVIEWER_ARBITER_RULES_BODY = (
    "a. Form your independent view from the visible evidence; the upstream "
    "docclass label (when present in handoff context) is routing state, not "
    "ground truth.\n"
    "b. Apply the family discriminators when weighing which reading reflects "
    "the document's real form: acquisition machinery (Parent/Merger Sub, "
    "Effective Time, Exchange Ratio) -> merger_agreement, not contract; claim "
    "documentation (FNOL, adjuster reports, demand packages, coverage "
    "determinations, denial letters) -> insurance_claim; records EMBEDDED as "
    "exhibits never change the parent agreement's class.\n"
    "c. Flag suspected upstream misclassification explicitly rather than "
    "silently re-reading the document into the assigned class's schema.\n"
    "The output-format requirements of the prompt above are unchanged."
)

_MARK = "(KANBAN-090)"


def _append(base: str, body: str, marker: str) -> str:
    """Pure-appended docclass variant: base is a STRICT PREFIX of the result."""
    return (
        base.rstrip("\n")
        + "\n\n" + _DOCCONTEXT + _rules(body)
        + f"\nDocclass variant: {marker} {_MARK}."
    )


# (production agent_name, docclass key, role-rules body)
_DOCCLASS_FROM_PRODUCTION: tuple[tuple[str, str, str], ...] = (
    ("sorter", "sorter_docclass_v0", _SORTER_RULES_BODY),
    ("contracts_specialist", "contracts_specialist_docclass_v0", _CONTRACTS_SPECIALIST_RULES_BODY),
    ("corporate_records_specialist", "corporate_records_specialist_docclass_v0", _SPECIALIST_RULES_BODY),
    ("correspondence_specialist", "correspondence_specialist_docclass_v0", _SPECIALIST_RULES_BODY),
    ("compliance_specialist", "compliance_specialist_docclass_v0", _SPECIALIST_RULES_BODY),
    ("insurance_claims_specialist", "insurance_claims_specialist_docclass_v0", _SPECIALIST_RULES_BODY),
    ("sorter_reviewer", "reviewer_docclass_v0", _REVIEWER_ARBITER_RULES_BODY),
    ("arbiter", "arbiter_docclass_v0", _REVIEWER_ARBITER_RULES_BODY),
    ("boss", "boss_docclass_v0", _BOSS_RULES_BODY),
    ("judge", "judge_docclass_v0", _JUDGE_RULES_BODY),
    ("judge-classification", "judge_classification_docclass_v0", _JUDGE_CLASSIFICATION_RULES_BODY),
    ("judge-correctness", "judge_correctness_docclass_v0", _JUDGE_RULES_BODY),
)


def _build_versions() -> dict[str, str]:
    """Derive every variant from the live production template of that role."""
    from llm.prompts import prompt_templates

    templates = prompt_templates()
    versions: dict[str, str] = {}
    missing = []
    for agent_name, key, body in _DOCCLASS_FROM_PRODUCTION:
        base = templates.get(agent_name)
        if not base:
            missing.append(agent_name)
            continue
        versions[key] = _append(base, body, key)
    if missing:
        raise RuntimeError(
            "docclass derivation missing production templates: "
            + ", ".join(missing)
        )
    return versions


DOCCLASS_PROMPT_VERSIONS: dict[str, str] = _build_versions()

# Named aliases kept so importers that pinned the V0 constants still resolve.
SORTER_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["sorter_docclass_v0"]
CONTRACTS_SPECIALIST_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["contracts_specialist_docclass_v0"]
CORPORATE_RECORDS_SPECIALIST_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["corporate_records_specialist_docclass_v0"]
CORRESPONDENCE_SPECIALIST_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["correspondence_specialist_docclass_v0"]
COMPLIANCE_SPECIALIST_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["compliance_specialist_docclass_v0"]
INSURANCE_CLAIMS_SPECIALIST_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["insurance_claims_specialist_docclass_v0"]
REVIEWER_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["reviewer_docclass_v0"]
ARBITER_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["arbiter_docclass_v0"]
BOSS_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["boss_docclass_v0"]
JUDGE_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["judge_docclass_v0"]
JUDGE_CLASSIFICATION_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["judge_classification_docclass_v0"]
JUDGE_CORRECTNESS_DOCCLASS_PROMPT_V0 = DOCCLASS_PROMPT_VERSIONS["judge_correctness_docclass_v0"]
