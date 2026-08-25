"""Production pipeline doctrine appended onto frozen predecessor prompts.

Each fragment is a smallest-testable mutation targeting a named failure class
observed in the mailroom pipeline (unknown-type remapping, missing CUAD
subtype, numeric-zero treated as empty, mixed-class false conflicts, vision
subtracting text, schema-field drift). Predecessors stay byte-identical;
new production versions are pure appends of these blocks.

Do not put Mustache placeholders (`{{...}}`) here — only the sorter base
carries injected taxonomy variables.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Shared failure-class rules
# ---------------------------------------------------------------------------

NUMERIC_ZERO = (
    "Numeric zero (0, 0.0, $0, $0.00) is a stated value, not absence. "
    "Use null or an empty list only when the document does not state the field."
)

VISION_ADDITIVE = (
    "When page images are attached they are supplementary. The full document "
    "text remains the primary evidence; never drop or ignore text because "
    "images are present."
)

GROUNDED = (
    "Extract only facts the document states. Do not invent parties, dates, "
    "amounts, holdings, or determinations from letterhead, filename, or "
    "general legal knowledge."
)

UNKNOWN_TYPE = (
    "If the document matches none of the configured class keys, set doc_type "
    "to unknown and contract_subtype to null. Never substitute correspondence "
    "or any other class for an unknown, empty, or invented type."
)

CUAD_SUBTYPE = (
    "When doc_type is contract, contract_subtype is required: pick exactly one "
    "key from the supplied subgroup list, or other when none fit. A missing "
    "or invented subtype is an incomplete classification."
)

SEVEN_CLASSES = (
    "The mailroom taxonomy has five primary classes: contract, "
    "corporate_record, correspondence, compliance_filing, "
    "insurance_claim. A demand letter about a contract is "
    "correspondence; an insurance policy is contract; FNOL/adjuster/"
    "coverage-denial paperwork is insurance_claim. A court opinion or "
    "due-diligence checklist/memo is not a mailroom class — set doc_type "
    "to unknown rather than remapping it onto correspondence or contract."
)

HANDOFF_IS_ROUTING = (
    "Classification (doc_type, contract_subtype) in any handoff is pipeline "
    "routing state, not ground truth and not an extraction field. Verify it "
    "against the visible text; extract the registered schema from the "
    "document as it actually reads."
)


def _block(title: str, lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return f"{title}\n{body}"


def extraction_doctrine(schema_fields: str, role_rules: list[str]) -> str:
    """Shared extraction closer for specialist SYSTEM_PROMPT mutations."""
    lines = [
        GROUNDED,
        NUMERIC_ZERO,
        VISION_ADDITIVE,
        HANDOFF_IS_ROUTING,
        f"Registered schema fields: {schema_fields}. Return every key; unstated values are null or [].",
        *role_rules,
    ]
    return _block("PRODUCTION DOCTRINE (mailroom pipeline):", lines)


def classification_doctrine(extra: list[str] | None = None) -> str:
    lines = [
        SEVEN_CLASSES,
        UNKNOWN_TYPE,
        CUAD_SUBTYPE,
        VISION_ADDITIVE,
        "Classify the document's substantive form, not the source wrapper, exhibit stamp, or filing context.",
        *(extra or []),
    ]
    return _block("PRODUCTION DOCTRINE (mailroom pipeline):", lines)


# ---------------------------------------------------------------------------
# Per-role doctrine (appended after the frozen predecessor)
# ---------------------------------------------------------------------------

SORTER = classification_doctrine(
    [
        "Output only a configured class key or unknown — never a paraphrase or a nearby class.",
    ]
)

SORTER_REVIEWER = classification_doctrine(
    [
        "You are blind to any prior classification. Form an independent view from the visible evidence only.",
        "If you cannot defend a single class, lower confidence rather than inventing a fit.",
    ]
)

CONTRACTS = extraction_doctrine(
    "document_name, parties, effective_date, term_length, termination_clauses, "
    "governing_law, key_obligations, contract_value, renewal_terms",
    [
        "parties is an entity list of distinct named parties; do not invent from letterhead without contract language.",
        "contract_value may be $0; that is a stated amount.",
        "CUAD subtype in the handoff selects expected clause families — it is not a schema field to emit.",
        "The per-field reasoning trace is evidence about how a value was found, not clause content.",
    ],
)

CORPORATE_RECORDS = extraction_doctrine(
    "entity_name, record_type, effective_date, key_provisions, signatories, "
    "jurisdiction, filing_number",
    [
        "entity_name is the legal name as written — do not abbreviate unless the document does.",
        "filing_number is an identifier; transcribe it exactly.",
        "A record embedded as an exhibit of a parent agreement does not change the parent; extract THIS document's fields.",
    ],
)

CORRESPONDENCE = extraction_doctrine(
    "sender, recipient, additional_recipients, communication_type, "
    "communication_date, key_points, demand_amount, action_items, urgency, "
    "referenced_communications",
    [
        "A demand letter about a contract is still correspondence. demand_amount of 0 is a stated amount.",
        "communication_date is the date sent, not a referenced deadline.",
        "Neutral tone defaults to urgency 'routine', not null.",
    ],
)

COMPLIANCE = extraction_doctrine(
    "filing_type, regulatory_body, filing_date, due_date, entity_name, "
    "key_requirements, status, reference_number",
    [
        "Name the filing type specifically (for example 10-K annual report, not merely SEC filing).",
        "reference_number is an identifier (accession, control, file number); transcribe it exactly.",
        "An agreement filed as an SEC exhibit is still extracted as a filing only when THIS document's form is the filing wrapper; do not pull the exhibit's contract fields into this schema.",
    ],
)

INSURANCE_CLAIMS = extraction_doctrine(
    "claim_number, policy_number, insurer, insured_party, claim_type, "
    "date_of_loss, date_filed, claimed_amount, adjuster, damages_description, "
    "coverage_determination, denial_reasons, supporting_documents",
    [
        "claim_number and policy_number are identifiers; never paraphrase them.",
        "claimed_amount of 0 is a stated amount. Do not compute or convert amounts.",
        "coverage_determination only as written (approved, denied, partial, pending); never infer a denial.",
        "An insurance POLICY sold to the insured is a contract, not this schema — if you are reading a policy, still fill only claim-documentation fields that the text actually states.",
    ],
)

BOSS = _block(
    "PRODUCTION DOCTRINE (mailroom pipeline):",
    [
        "Matter conflicts are same-class only. Shared field names across different document classes (for example effective_date on a contract and a corporate record) are not a conflict.",
        "A leftover review_decision of approved from an earlier resume is not your ruling. Decide from the current escalation evidence.",
        SEVEN_CLASSES,
        "If both extractions are internally consistent but describe materially different document forms, prefer review and name the suspected misclassification.",
        "Be decisive: approved proceeds to compile_report; review parks for a human. Return one complete JSON object for the active role's schema.",
    ],
)

REPORTER = _block(
    "PRODUCTION DOCTRINE (mailroom pipeline):",
    [
        NUMERIC_ZERO,
        "Preserve every extracted field that has a stated value, including 0. Do not drop sparse extractions.",
        "Do not extract new facts. If extracted_data is missing or empty, say so; do not invent a matter record from classification alone.",
        "Confidence reflects the quality of the underlying extraction, not a default high score.",
    ],
)

PDF_TRANSCRIBER = _block(
    "PRODUCTION DOCTRINE (mailroom pipeline):",
    [
        "Transcribe; do not summarize, classify, or extract fields.",
        "If the source is already selectable text, clean structure without inventing wording.",
        "Illegible or garbled spans are [corrupted text] or [UNREADABLE], never guessed words.",
        VISION_ADDITIVE,
    ],
)

JUDGE_COMPLETENESS = _block(
    "PRODUCTION DOCTRINE (mailroom pipeline):",
    [
        NUMERIC_ZERO + " A populated 0 is not an empty field.",
        "Judge only the registered schema for the assigned class. Do not demand another class's fields.",
        SEVEN_CLASSES,
        VISION_ADDITIVE,
    ],
)

JUDGE_CLASSIFICATION = classification_doctrine(
    [
        "Grade doc_type and, for contracts, contract_subtype. A correct class with a missing subtype is not fully correct.",
        "unknown is a valid assigned type when the document fits none of the five live classes; do not mark that incorrect merely because a nearby class exists.",
    ]
)

JUDGE_CORRECTNESS = _block(
    "PRODUCTION DOCTRINE (mailroom pipeline):",
    [
        NUMERIC_ZERO,
        "Judge only registered schema fields. Identifiers (claim numbers, docket numbers, accession numbers) must match as printed.",
        "Empty/null is correct when the visible source does not state the fact; a stated 0 is a value to verify.",
        VISION_ADDITIVE,
    ],
)

ARBITER = _block(
    "PRODUCTION DOCTRINE (mailroom pipeline):",
    [
        "fields_to_fix must be registered schema field names for this document's class, never commentary.",
        "retry_extraction is for a small named set of recoverable fields; human_review when failures compound or the source is materially unreadable.",
        NUMERIC_ZERO + " Do not treat a stated 0 as a missed field.",
        SEVEN_CLASSES,
        "Default to the least destructive sufficient action. Return one complete JSON object.",
    ],
)
