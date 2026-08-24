"""Docclass prompt variants for every mailroom classification-chain role.

KANBAN-090 (2026-08-23, human directive via Discord #hermes): mirrors the
llm-entity-extraction docclass prompt arm (``src/prompts_docclass.py`` there).
Every variant here is DERIVED from this repo's own production base constant by
PURE APPENDITION — ``variant.startswith(base)`` holds in full, the base bytes
are untouched, and the docclass block rides after the base's own JSON-output
closer as additive context. Nothing is replaced, so no anchor can drift.

    role                          key in DOCCLASS_PROMPT_VERSIONS
    -----------------------------  --------------------------------------
    contracts_specialist           contracts_specialist_docclass_v0
    corporate_records_specialist   corporate_records_specialist_docclass_v0
    due_diligence_specialist       due_diligence_specialist_docclass_v0
    correspondence_specialist      correspondence_specialist_docclass_v0
    compliance_specialist          compliance_specialist_docclass_v0
    court_opinions_specialist      court_opinions_specialist_docclass_v0
    insurance_claims_specialist    insurance_claims_specialist_docclass_v0
    reviewer (second opinion)      reviewer_docclass_v0
    arbiter                        arbiter_docclass_v0
    boss                           boss_docclass_v0
    judge (completeness)           judge_docclass_v0
    judge (classification)         judge_classification_docclass_v0
    judge (correctness)            judge_correctness_docclass_v0

DEPLOYMENT MODEL DIFFERENCE (vs entity): mailroom's Langfuse production
surface is THIRTEEN agent-name-pinned templates (`mailroom-<agent_name>`,
see llm/prompts.py::prompt_templates — the count is test-pinned). Docclass
variants must NEVER flow through prompt_templates(): that would overwrite
production agent prompts. They ship as this standalone registry and reach
Langfuse only via the OPT-IN sync path::

    python scripts/sync_prompts.py --docclass

which pushes them under distinct ``mailroom-docclass-<key>`` names,
content-keyed and idempotent like every other sync. Runtime routes are
untouched: no pipeline fetches a docclass key by default.
"""

from __future__ import annotations

from langchain_agents.prompts import (
    COMPLIANCE_SPECIALIST_PROMPT,
    CONTRACTS_SPECIALIST_PROMPT,
    CORRESPONDENCE_SPECIALIST_PROMPT,
    COURT_OPINIONS_SPECIALIST_PROMPT,
    CORPORATE_RECORDS_SPECIALIST_PROMPT,
    DUE_DILIGENCE_SPECIALIST_PROMPT,
)
from agents.arbiter import ARBITER_SYSTEM_PROMPT
from agents.boss import BOSS_SYSTEM_PROMPT
from agents.insurance_claims_specialist import SYSTEM_PROMPT as INSURANCE_CLAIMS_SYSTEM_PROMPT
from agents.judge import (
    CLASSIFICATION_SYSTEM_PROMPT,
    CORRECTNESS_SYSTEM_PROMPT,
    SYSTEM_PROMPT as JUDGE_COMPLETENESS_SYSTEM_PROMPT,
)
from agents.sorter_reviewer import REVIEWER_SYSTEM_PROMPT

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


CONTRACTS_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    CONTRACTS_SPECIALIST_PROMPT, _SPECIALIST_RULES_BODY, "contracts_specialist_docclass_v0",
)
CORPORATE_RECORDS_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    CORPORATE_RECORDS_SPECIALIST_PROMPT, _SPECIALIST_RULES_BODY, "corporate_records_specialist_docclass_v0",
)
DUE_DILIGENCE_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    DUE_DILIGENCE_SPECIALIST_PROMPT, _SPECIALIST_RULES_BODY, "due_diligence_specialist_docclass_v0",
)
CORRESPONDENCE_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    CORRESPONDENCE_SPECIALIST_PROMPT, _SPECIALIST_RULES_BODY, "correspondence_specialist_docclass_v0",
)
COMPLIANCE_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    COMPLIANCE_SPECIALIST_PROMPT, _SPECIALIST_RULES_BODY, "compliance_specialist_docclass_v0",
)
COURT_OPINIONS_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    COURT_OPINIONS_SPECIALIST_PROMPT, _SPECIALIST_RULES_BODY, "court_opinions_specialist_docclass_v0",
)
INSURANCE_CLAIMS_SPECIALIST_DOCCLASS_PROMPT_V0 = _append(
    INSURANCE_CLAIMS_SYSTEM_PROMPT, _SPECIALIST_RULES_BODY, "insurance_claims_specialist_docclass_v0",
)
REVIEWER_DOCCLASS_PROMPT_V0 = _append(
    REVIEWER_SYSTEM_PROMPT, _REVIEWER_ARBITER_RULES_BODY, "reviewer_docclass_v0",
)
ARBITER_DOCCLASS_PROMPT_V0 = _append(
    ARBITER_SYSTEM_PROMPT, _REVIEWER_ARBITER_RULES_BODY, "arbiter_docclass_v0",
)
BOSS_DOCCLASS_PROMPT_V0 = _append(
    BOSS_SYSTEM_PROMPT, _BOSS_RULES_BODY, "boss_docclass_v0",
)
JUDGE_DOCCLASS_PROMPT_V0 = _append(
    JUDGE_COMPLETENESS_SYSTEM_PROMPT, _JUDGE_RULES_BODY, "judge_docclass_v0",
)
JUDGE_CLASSIFICATION_DOCCLASS_PROMPT_V0 = _append(
    CLASSIFICATION_SYSTEM_PROMPT, _JUDGE_CLASSIFICATION_RULES_BODY, "judge_classification_docclass_v0",
)
JUDGE_CORRECTNESS_DOCCLASS_PROMPT_V0 = _append(
    CORRECTNESS_SYSTEM_PROMPT, _JUDGE_RULES_BODY, "judge_correctness_docclass_v0",
)

DOCCLASS_PROMPT_VERSIONS: dict[str, str] = {
    "contracts_specialist_docclass_v0": CONTRACTS_SPECIALIST_DOCCLASS_PROMPT_V0,
    "corporate_records_specialist_docclass_v0": CORPORATE_RECORDS_SPECIALIST_DOCCLASS_PROMPT_V0,
    "due_diligence_specialist_docclass_v0": DUE_DILIGENCE_SPECIALIST_DOCCLASS_PROMPT_V0,
    "correspondence_specialist_docclass_v0": CORRESPONDENCE_SPECIALIST_DOCCLASS_PROMPT_V0,
    "compliance_specialist_docclass_v0": COMPLIANCE_SPECIALIST_DOCCLASS_PROMPT_V0,
    "court_opinions_specialist_docclass_v0": COURT_OPINIONS_SPECIALIST_DOCCLASS_PROMPT_V0,
    "insurance_claims_specialist_docclass_v0": INSURANCE_CLAIMS_SPECIALIST_DOCCLASS_PROMPT_V0,
    "reviewer_docclass_v0": REVIEWER_DOCCLASS_PROMPT_V0,
    "arbiter_docclass_v0": ARBITER_DOCCLASS_PROMPT_V0,
    "boss_docclass_v0": BOSS_DOCCLASS_PROMPT_V0,
    "judge_docclass_v0": JUDGE_DOCCLASS_PROMPT_V0,
    "judge_classification_docclass_v0": JUDGE_CLASSIFICATION_DOCCLASS_PROMPT_V0,
    "judge_correctness_docclass_v0": JUDGE_CORRECTNESS_DOCCLASS_PROMPT_V0,
}
