# VENDORED from github.com/Exios66/llm-entity-extraction (re-vendored to the
# sibling's current HEAD, 2026-08-15 — adds the v24+ per-field ``reasoning``
# trace schema and the v15+ chunked-extraction pass since the 3a03d5c pin).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""Specialist agents for field extraction from each document type (LangChain).

Each specialist knows how to extract fields specific to its document type and
is driven by a versioned prompt from ``src.prompts``. Schemas are exported as
module constants so the eval loops and judges can reference the same contracts.
"""

from __future__ import annotations

from typing import Literal

import structlog
from langchain_agents.base_agent import BaseAgent, build_structured_schema
from langchain_agents.doc_inventories import (
    CLAIM_TYPE_DESCRIPTION,
    COMMUNICATION_TYPE_DESCRIPTION,
    INTENT_DESCRIPTIONS,
    RECORD_TYPE_DESCRIPTION,
)
from langchain_agents.prompts import get_prompt

logger = structlog.get_logger(__name__)


_FAILED_CHUNK_MARGIN = 0.01
_FAILED_CHUNK_FALLBACK_CEILING = 0.69


def failed_chunk_ceiling(agent_name: str) -> float:
    """Confidence ceiling for a chunked extraction that lost a window.

    Strictly below the ``low`` threshold of every doc class this specialist
    serves (taxonomy ``doc_classes[].specialist`` + ``confidence.by_class``), so
    routing retries / escalates instead of archiving a partial extraction.
    MAILROOM PATCH — not in the upstream vendored pass.
    """
    try:
        from pipeline.config import get_confidence_thresholds, load_config

        lows = [
            float(get_confidence_thresholds(cls["key"]).get("low", 0.70))
            for cls in (load_config().get("doc_classes") or [])
            if cls.get("specialist") == agent_name
        ]
        if not lows:
            lows = [float(get_confidence_thresholds().get("low", 0.70))]
        return round(min(lows) - _FAILED_CHUNK_MARGIN, 4)
    except Exception:  # noqa: BLE001 - config trouble must not discard paid-for chunks
        return _FAILED_CHUNK_FALLBACK_CEILING


def finalize_chunked_merge(
    merged: dict,
    *,
    agent_name: str,
    n_chunks: int,
    failed: int,
    window_confidences: list | None = None,
    window_has_fields: list | None = None,
) -> dict:
    """Stamp chunk bookkeeping on a merged extraction and cap it on any loss.

    ``_n_chunks`` / ``_failed_chunks`` are underscore (non-public) keys: field
    scoring, the hollow check and reconsideration all ignore them. When at
    least one window failed (parse error / non-transient exception), the
    merged confidence is capped strictly below the class ``low`` threshold so
    a partial extraction is retried or escalated, never auto-archived.
    ``_window_confidences`` / ``_window_has_fields`` carry the per-window evidence
    (windows that parsed, in document order) so a sweep can re-derive the merge
    under another rule; ``graph.build_graph._run_chunked_extraction`` strips them
    before the payload reaches state, so they never reach the archive.
    MAILROOM PATCH.
    """
    merged["_n_chunks"] = n_chunks
    merged["_failed_chunks"] = failed
    if window_confidences is not None and window_has_fields is not None:
        merged["_window_confidences"] = list(window_confidences)
        merged["_window_has_fields"] = list(window_has_fields)
    if failed:
        ceiling = failed_chunk_ceiling(agent_name)
        current = merged.get("confidence")
        current = float(current) if isinstance(current, (int, float)) else ceiling
        merged["confidence"] = round(min(current, ceiling), 4)
        logger.warning(
            "chunked_extraction_partial",
            agent=agent_name,
            failed_chunks=failed,
            n_chunks=n_chunks,
            confidence=merged["confidence"],
        )
    return merged


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def merge_window_confidences(
    confidences: list, has_fields: list, rule: Literal["min", "cr6"]
):
    """Merged confidence of the windows that parsed (pure; live merge and replay share it).

    ``min``: the weakest window, whatever it produced.
    ``cr6``: the weakest window that produced fields. A window with NO fields
    (signature page, exhibit) says nothing about extraction quality, so it is
    ignored whenever any other window holds fields; when none does, or none of
    the productive windows states a confidence, the plain minimum applies (a
    document that yields nothing stays low). ``None`` when no window states a
    confidence. The failed-window cap is NOT part of this rule
    (``finalize_chunked_merge`` / ``policy_replay`` apply it afterwards).
    """
    if rule not in ("min", "cr6"):
        raise ValueError(f"unknown merge rule {rule!r}")
    if len(confidences) != len(has_fields):
        raise ValueError("confidences and has_fields must have the same length")
    pool = [c for c in confidences if _is_number(c)]
    if rule == "cr6":
        productive = [c for c, h in zip(confidences, has_fields) if h and _is_number(c)]
        if productive:
            pool = productive
    return min(pool) if pool else None


def _window_confidence(acc: dict, chunk: dict):
    """Confidence of ``acc`` merged with the next window ``chunk`` (rule ``cr6``).

    Windows that extracted fields are combined by their weakest confidence (a
    document is only as reliable as its weakest productive window). A window
    that produced NO fields (signature page, exhibit, boilerplate) says nothing
    about extraction quality, only that the model found nothing there, and its
    low stated confidence used to drag a clean document below the retry line.
    It is ignored whenever the other side holds fields; when neither does, the
    plain minimum applies (a document that yields nothing stays low). A window
    that FAILED never reaches the merge: ``finalize_chunked_merge`` caps the
    result below the class ``low`` threshold for it, independent of this rule.
    "No fields" is exactly what routing calls hollow, so the two agree. The rule
    itself lives in :func:`merge_window_confidences`.
    """
    from pipeline.reconsideration import extraction_is_hollow

    return merge_window_confidences(
        [acc.get("confidence"), chunk.get("confidence")],
        [not extraction_is_hollow(acc), not extraction_is_hollow(chunk)],
        "cr6",
    )


def window_evidence(result: dict) -> tuple:
    """``(stated confidence or None, produced fields)`` of one parsed window."""
    from pipeline.reconsideration import extraction_is_hollow

    conf = result.get("confidence")
    return (conf if _is_number(conf) else None), not extraction_is_hollow(result)


def _norm(text: str) -> str:
    """Normalize clause text for dedupe: whitespace-collapse + casefold.

    The chunk overlap window re-quotes a clause verbatim, so a
    whitespace/case-insensitive comparison makes the duplicate a no-op.
    """
    if text is None:
        return ""
    return " ".join(str(text).split()).casefold()


def _merge_reasoning(acc, chunk_reasoning) -> dict:
    """Union two per-chunk reasoning traces into one.

    Entries dedupe by field name (first-witness evidence + section reference
    win — the chunk that first located the value holds its evidence); the
    summaries join in chunk order with a marker so the merged trace covers
    every window. A missing side degrades gracefully (None-safe).
    """
    acc = acc if isinstance(acc, dict) else {}
    chunk_reasoning = chunk_reasoning if isinstance(chunk_reasoning, dict) else {}

    entries: dict[str, dict] = {}
    for entry in list(acc.get("entries") or []) + list(chunk_reasoning.get("entries") or []):
        if not isinstance(entry, dict) or not entry.get("field"):
            continue
        entries.setdefault(entry["field"], entry)

    summaries = [s for s in (acc.get("summary"), chunk_reasoning.get("summary")) if s]
    return {
        "summary": "\n\n".join(summaries) if summaries else "",
        "entries": list(entries.values()),
    }


def _nullable_string(description: str = "") -> dict:
    return {"type": ["string", "null"], "description": description}


def _nullable_number(description: str = "") -> dict:
    # MAILROOM PATCH: amount fields are floats in the Pydantic models; the
    # request schema must say number (not string) so the model emits a number
    # and the guardrail's model_validate cannot disagree on the type.
    return {"type": ["number", "null"], "description": description}


def _string_array(description: str = "") -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def normalize_extraction(result: dict, schema: dict) -> dict:
    """Guarantee the extraction carries EVERY schema field.

    The model occasionally omits a field (e.g. ``confidence``) or returns a
    malformed shape. This fills missing keys with their schema defaults
    (null for nullable strings and nullable numbers, [] for arrays, 0.0 for
    non-nullable numbers) so downstream scoring and reporting always see a
    complete, conformant extraction.
    """
    normalized = dict(result or {})
    for key, spec in (schema.get("properties") or {}).items():
        if key in normalized and normalized[key] not in (None, ""):
            continue
        type_spec = spec.get("type")
        nullable = isinstance(type_spec, list) and "null" in type_spec
        if isinstance(type_spec, list):
            type_spec = next((t for t in type_spec if t != "null"), type_spec[0])
        if type_spec == "array":
            normalized[key] = normalized.get(key) or []
        elif type_spec == "number":
            # MAILROOM PATCH: a nullable number (demand_amount / claimed_amount)
            # defaults to null so an unstated amount is never coerced into a
            # stated 0. Non-nullable numbers (confidence) keep the 0.0 default.
            value = normalized.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                normalized[key] = value
            else:
                normalized[key] = None if nullable else 0.0
        else:
            normalized[key] = normalized.get(key) if normalized.get(key) not in (None, "") else None
    return normalized


# =============================================================================
# Extraction schemas (single source of truth for specialists + judges)
# =============================================================================

_CONFIDENCE = {
    "type": "number", "minimum": 0.0, "maximum": 1.0,
    "description": "Evidence-grounded extraction confidence (share of fields found, "
                    "lowered by uncertain values or truncation; never a fixed default)",
}

CONTRACTS_SCHEMA = build_structured_schema({
    "reasoning": {
        "type": "object",
        "description": "Per-field reasoning trace, produced BEFORE finalizing the "
                       "extraction: a summary of the scan plus one entry per populated "
                       "field naming the field, its evidence (short verbatim quote or "
                       "definition/alias note), and the section reference where it was "
                       "found. Describes HOW each value was found — never part of the "
                       "clause text and never replaces an extracted value.",
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
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "entries"],
        "additionalProperties": False,
    },
    "document_name": _nullable_string("The name of the contract (e.g. 'Web Hosting Agreement')"),
    "parties": _string_array("The names of the contracting parties"),
    # MAILROOM PATCH: v2 fields match schemas.documents.ContractExtraction.
    "party_roles": _string_array("Each contracting party and its role as '<party>: <role>'"),
    "signatories": _string_array("Persons who signed the contract, as printed"),
    "agreement_date": _nullable_string("The dated-as-of or signature date as YYYY-MM-DD"),
    "effective_date": _nullable_string("YYYY-MM-DD (ISO)"),
    "expiration_date": _nullable_string("End of the initial term as YYYY-MM-DD when stated as a date"),
    "term_length": _nullable_string("The full duration or term of the agreement, including any riders"),
    "termination_notice_period": _nullable_string("Notice needed to terminate or prevent renewal, as stated"),
    "governing_law": _nullable_string("The jurisdiction whose laws govern the agreement (governing-law sentence only)"),
    "dispute_forum": _nullable_string("Courts or arbitral seat for disputes, separate from governing law"),
    "contract_value": _nullable_string("The monetary value or consideration"),
    "payment_terms": _nullable_string("How and when the consideration is paid, as stated"),
    "territory": _nullable_string("The stated territory of the grant, distribution, or service"),
    "renewal_terms": _nullable_string("Renewal, extension, or rollover terms (automatic or otherwise)"),
    "cuad_family": _nullable_string(
        "CUAD agreement family key: affiliate, agency, collaboration, co_branding, "
        "consulting, development, distributor, endorsement, franchise, hosting, ip, "
        "joint_venture, license, maintenance, manufacturing, marketing, "
        "non_compete_no_solicit, outsourcing, promotion, reseller, service, "
        "sponsorship, strategic_alliance, supply, transportation, or other. Null for merger_agreement."
    ),
    "merger_consideration": _nullable_string(
        "MAUD merger consideration token: all_cash, all_stock, mixed_cash_stock, "
        "mixed_cash_stock_election, or other. Null when the document is not a merger."
    ),
    "cuad_clauses": _string_array(
        "Present CUAD clause categories (the 41 Atticus names) as "
        "'<Category>: <short verbatim evidence span>'. Omit absent categories. "
        "Do NOT dump open-ended obligation lists — answer the fixed CUAD checklist only."
    ),
    "maud_clauses": _string_array(
        "Answered MAUD questions as '<Question>: <Answer>' using the exact "
        "LegalBench MAUD names (Absence of Litigation Closing Condition, "
        "Accuracy of Target R&W Closing Condition, MAE Definition, No-Shop, "
        "Type of Consideration, …). Answer is the Hub valid_class, not a "
        "paraphrase. Empty unless this is a merger agreement."
    ),
    "confidence": {
        "type": "number", "minimum": 0.0, "maximum": 1.0,
        "description": "Evidence-grounded extraction confidence (share of fields found, "
                        "lowered by uncertain values or truncation; never a fixed default)",
    },
})

CORPORATE_RECORDS_SCHEMA = build_structured_schema({
    "entity_name": _nullable_string("Legal entity name as stated"),
    "record_type": _nullable_string(RECORD_TYPE_DESCRIPTION),
    "effective_date": _nullable_string("Date the record took effect (ISO or as written)"),
    "signatories": _string_array("Individuals who signed or approved"),
    "jurisdiction": _nullable_string("State/country of incorporation"),
    "filing_number": _nullable_string("Official filing or document reference number"),
    "intent": _nullable_string(INTENT_DESCRIPTIONS["corporate_record"]),
    "subject_matter": _nullable_string("One tight grounded sentence: what this record is about"),
    "keywords": _string_array(
        "Up to 8 salient terms/phrases grounded in the text (no invented topics)"
    ),
    "confidence": _CONFIDENCE,
})

CORRESPONDENCE_SCHEMA = build_structured_schema({
    "sender": _nullable_string("Who sent the communication"),
    "recipient": _nullable_string("Who received it"),
    "additional_recipients": _string_array("Cc'd or otherwise copied parties"),
    "communication_type": _nullable_string(COMMUNICATION_TYPE_DESCRIPTION),
    "communication_date": _nullable_string("Date the communication was sent"),
    "demand_amount": _nullable_number("Exact dollar amount demanded (demand letters only)"),
    "action_items": _string_array("At most 3 concrete actions required, with deadlines if stated"),
    "urgency": _nullable_string("Urgency level: routine, time-sensitive, urgent, critical"),
    "intent": _nullable_string(INTENT_DESCRIPTIONS["correspondence"]),
    "subject_matter": _nullable_string("One tight grounded sentence: what this communication is about"),
    "keywords": _string_array(
        "Up to 8 salient terms/phrases grounded in the text (no invented topics)"
    ),
    "confidence": _CONFIDENCE,
})

INSURANCE_CLAIMS_SCHEMA = build_structured_schema({
    "claim_number": _nullable_string("Claim number exactly as printed (CLAIM NO., FNOL ref., CLM_ID)"),
    "policy_number": _nullable_string("Policy number exactly as printed"),
    "insurer": _nullable_string("Named insurance company / carrier"),
    "insured_party": _nullable_string("Named insured or claimant"),
    "claim_type": _nullable_string(CLAIM_TYPE_DESCRIPTION),
    "date_of_loss": _nullable_string("Date the loss/event occurred, if stated"),
    "date_filed": _nullable_string("Date the claim was filed, if stated"),
    "claimed_amount": _nullable_number("Amount claimed/demanded, if stated"),
    "adjuster": _nullable_string("Named adjuster handling the claim, if stated; null when absent"),
    "damages_description": _nullable_string("Summary of the loss/damages as described"),
    "coverage_determination": _nullable_string("Outcome as stated: approved, denied, partial, pending"),
    "denial_reasons": _string_array("Stated denial/limitation grounds, if denied"),
    "supporting_documents": _string_array("Referenced supporting documents"),
    "intent": _nullable_string(INTENT_DESCRIPTIONS["insurance_claim"]),
    "subject_matter": _nullable_string("One tight grounded sentence: what this claim document is about"),
    "keywords": _string_array(
        "Up to 8 salient terms/phrases grounded in the text (no invented topics)"
    ),
    "claim_checklist": _string_array(
        "Present claim checklist answers as '<Category>: <short evidence>'. "
        "Categories: Coverage Determination, Policy Limits, Exclusions Cited, "
        "Deductible, Reservation Of Rights, Timely Notice, Proof Of Loss, "
        "Subrogation, Independent Medical Exam, Amount Consistency. Omit absent."
    ),
    "confidence": _CONFIDENCE,
})


def _merger_agreement_schema() -> dict:
    """Dedicated MAUD schema — reuses LegalBench question / consideration tokens."""
    from langchain_agents.cuad_maud import MAUD_CLAUSE_QUESTIONS, MAUD_CONSIDERATION

    questions = ", ".join(MAUD_CLAUSE_QUESTIONS)
    tokens = ", ".join(MAUD_CONSIDERATION)
    return build_structured_schema({
        "reasoning": {
            "type": "object",
            "description": "Per-field reasoning trace, produced BEFORE finalizing the "
                           "extraction: a summary of the scan plus one entry per populated "
                           "field naming the field, its evidence (short verbatim quote or "
                           "definition/alias note), and the section reference where it was "
                           "found. Describes HOW each value was found — never part of the "
                           "clause text and never replaces an extracted value.",
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
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "entries"],
            "additionalProperties": False,
        },
        "document_name": _nullable_string(
            "The name of the merger agreement (e.g. 'Agreement and Plan of Merger')"
        ),
        "parties": _string_array(
            "Named parties as stated — Parent, Merger Sub, and Target (and any "
            "other contracting entity the agreement names)"
        ),
        # MAILROOM PATCH: v2 fields match schemas.documents.MergerAgreementExtraction.
        "acquirer": _nullable_string("The Parent (acquirer), as printed"),
        "target": _nullable_string("The Company (target), as printed"),
        "merger_sub": _string_array("Merger subsidiary or subsidiaries, as printed"),
        "signatories": _string_array("Persons signing for each party, as printed"),
        "effective_date": _nullable_string("Effective Date as YYYY-MM-DD (ISO) when stated"),
        "effective_time": _nullable_string(
            "Effective Time as stated (clock time, time zone, or defined-term "
            "reference such as 'the Effective Time')"
        ),
        "governing_law": _nullable_string(
            "The jurisdiction whose laws govern the agreement (governing-law sentence only)"
        ),
        "per_share_cash_amount": _nullable_string("Cash paid per Target share, as stated"),
        "exchange_ratio": _nullable_string("Parent shares per Target share, as stated"),
        "target_termination_fee": _nullable_string("Termination fee payable by the Target, as stated"),
        "parent_termination_fee": _nullable_string("Reverse termination fee payable by the Parent, as stated"),
        "outside_date": _nullable_string("End or Outside Date as YYYY-MM-DD, or as printed when relative"),
        "financial_advisors": _string_array("Financial advisors named in the agreement"),
        "legal_counsel": _string_array("Law firms named in the agreement"),
        "merger_consideration": _nullable_string(
            "MAUD merger consideration token — exactly one of: " + tokens
        ),
        "maud_clauses": _string_array(
            "Answered MAUD questions as '<Question>: <Answer>' using the exact "
            "LegalBench MAUD names (" + questions + "). Answer is the Hub "
            "valid_class, not a paraphrase. Omit unanswered questions. "
            "Do NOT emit cuad_family or cuad_clauses — those are CUAD-only."
        ),
        "intent": _nullable_string(
            "Primary purpose as a short controlled label, e.g. effect_merger, "
            "amend_merger, plan_of_merger — one label, not a paragraph"
        ),
        "subject_matter": _nullable_string(
            "One tight grounded sentence: what this merger agreement is about"
        ),
        "keywords": _string_array(
            "Up to 8 salient terms/phrases grounded in the text (no invented topics)"
        ),
        "confidence": {
            "type": "number", "minimum": 0.0, "maximum": 1.0,
            "description": "Evidence-grounded extraction confidence (share of fields found, "
                            "lowered by uncertain values or truncation; never a fixed default)",
        },
    })


MERGER_AGREEMENT_SCHEMA = _merger_agreement_schema()

SPECIALIST_SCHEMAS = {
    "contract": CONTRACTS_SCHEMA,
    "merger_agreement": MERGER_AGREEMENT_SCHEMA,
    "corporate_record": CORPORATE_RECORDS_SCHEMA,
    "correspondence": CORRESPONDENCE_SCHEMA,
    "insurance_claim": INSURANCE_CLAIMS_SCHEMA,
}


def get_extraction_schema(doc_type: str) -> dict | None:
    """Return the extraction JSON schema for a doc type (None if unknown)."""
    if doc_type in SPECIALIST_SCHEMAS:
        return SPECIALIST_SCHEMAS[doc_type]
    try:
        from pipeline.config import resolve_extract_class

        resolved = resolve_extract_class(doc_type)
        if resolved:
            return SPECIALIST_SCHEMAS.get(resolved)
    except ImportError:
        pass
    return None


# =============================================================================
# Specialist agents
# =============================================================================


class _SpecialistBase(BaseAgent):
    """Shared extract() implementation over a per-class schema."""

    schema: dict
    handoff_context: str | None = None

    # ------------------------------------------------------------------
    # Chunked extraction pass (v15+ architectural layer)
    # ------------------------------------------------------------------
    # Contracts up to 335k chars exceed any single-call input budget, and
    # head+tail truncation drops the MIDDLE — exactly where obligation
    # families concentrate. Chunked mode splits the document on paragraph
    # boundaries into overlapping windows, extracts each window in its own
    # call, and merges: list fields union with normalized dedupe (overlap
    # re-quotes the same clause), scalars keep the first non-null value,
    # confidence takes the MIN (a document is only as reliable as its weakest
    # window; MAILROOM PATCH). Nothing is truncated; the merge is the
    # completeness guarantee. A window that fails to parse is counted and caps
    # the merged confidence below the class ``low`` threshold; a transient
    # provider error is re-raised so the node's transient routing retries.
    # ------------------------------------------------------------------

    def extract_chunked(
        self,
        doc_text: str,
        chunk_chars: int = 90_000,
        overlap_chars: int = 8_000,
        pages: list[str] | None = None,  # MAILROOM PATCH: page-image data-URIs
    ) -> dict:
        """Extract a long document in overlapping chunks and merge the passes.

        Documents that fit in a single window take the plain single-pass
        path (``extract``) — identical behavior to non-chunked mode, so
        chunking can never change small-document output. Longer documents
        are split, each window extracted in its own call, and merged: list
        fields union with normalized dedupe, scalars keep the first non-null
        value, confidence takes the MIN over windows that produced fields. A chunk that fails to parse (or
        raises a non-transient error) is counted in ``_failed_chunks`` and caps
        the merged confidence below the class ``low`` threshold; a transient
        provider error is re-raised so the node retries instead of archiving
        a partial extraction.

        MAILROOM PATCH: page-image data-URIs are attached only to the FIRST
        chunk — the model sees the full document pages once plus every text
        window (the additive vision guarantee at bounded cost instead of
        multiplying image attachments per chunk).
        """
        chunks = self._split_chunks(doc_text, chunk_chars, overlap_chars)
        self._last_n_chunks = len(chunks)
        if len(chunks) == 1:
            return self.extract(doc_text, pages=pages)
        merged: dict | None = None
        total_usage: dict | None = None
        failed = 0
        window_confs: list = []
        window_fields: list = []
        self._last_window_confidences = self._last_window_has_fields = None
        for index, chunk in enumerate(chunks, start=1):
            header = (f"EXTRACTION CHUNK {index} OF {len(chunks)} — this is one "
                      f"window of the document; extract every field occurrence "
                      f"present in THIS chunk (see the system prompt's chunk duty).\n")
            user_message = (
                f"{header}Extract fields from this {self._doc_label} document (chunk "
                f"{index} of {len(chunks)}):\n\n{chunk}"
            )
            if self.handoff_context:
                user_message = f"{self.handoff_context}\n\n{user_message}"
            try:
                result = self._call_structured(
                    user_message,
                    json_schema=self.schema,
                    temperature=0.1,
                    pages=pages if index == 1 else None,  # MAILROOM PATCH
                )
            except Exception as exc:  # noqa: BLE001 - non-transient chunk failure is counted
                from llm.retry import is_transient_error

                if is_transient_error(exc):
                    raise
                logger.warning("chunk_call_failed", agent=self.agent_name,
                               chunk=index, total=len(chunks), error=str(exc)[:200])
                failed += 1
                continue
            if self._last_usage:
                total_usage = self._sum_usage(total_usage, self._last_usage)
            if result.get("_parse_error"):
                failed += 1
                continue
            if self._confidence_missing(result):
                result["confidence"] = round(self._evidence_confidence(result), 4)
            result = normalize_extraction(result, self.schema)
            # Per-window evidence is read BEFORE the merge: the merge unions lists
            # into the accumulator's own lists, which would change a window's
            # "has fields" after the fact.
            conf, has_fields = window_evidence(result)
            window_confs.append(conf)
            window_fields.append(has_fields)
            merged = result if merged is None else self._merge_extractions(merged, result)
        self._last_usage = total_usage
        self._last_truncated = False
        self._last_chunked = True
        self._last_failed_chunks = failed
        self._last_window_confidences, self._last_window_has_fields = window_confs, window_fields
        if merged is None:
            return {"_parse_error": True}
        return finalize_chunked_merge(
            merged, agent_name=self.agent_name, n_chunks=len(chunks), failed=failed,
            window_confidences=window_confs, window_has_fields=window_fields,
        )

    @staticmethod
    def _sum_usage(acc: dict | None, usage: dict) -> dict:
        """Sum per-chunk usage dicts (prompt/completion/total tokens, cost)."""
        merged = dict(acc or {})
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            merged[key] = (merged.get(key) or 0) + int(usage.get(key) or 0)
        cost = usage.get("cost")
        if cost is not None:
            merged["cost"] = (merged.get("cost") or 0.0) + float(cost)
        return merged

    @staticmethod
    def _split_chunks(text: str, chunk_chars: int,
                      overlap_chars: int) -> list[str]:
        """Paragraph-aware chunking with a trailing overlap window.

        Paragraphs (``\\n\\n``) are kept intact; a single paragraph larger
        than the budget is hard-split on sentence-ish boundaries. Every chunk
        after the first is prepended with the previous chunk's tail so a
        clause crossing the cut is visible on both sides (the merge dedupes).
        """
        if len(text) <= chunk_chars:
            return [text]
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for para in paragraphs:
            while len(para) > chunk_chars:  # pathological single paragraph
                chunks.append(para[:chunk_chars])
                para = para[chunk_chars:]
            if current and current_len + len(para) + 2 > chunk_chars:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            current.append(para)
            current_len += len(para) + 2
        if current:
            chunks.append("\n\n".join(current))
        if len(chunks) > 1 and overlap_chars > 0:
            overlapped = [chunks[0]]
            for i in range(1, len(chunks)):
                tail = chunks[i - 1][-overlap_chars:]
                if "\n\n" in tail:
                    tail = tail[tail.find("\n\n") + 2:]
                overlapped.append(f"{tail}\n\n{chunks[i]}")
            chunks = overlapped
        return chunks

    @staticmethod
    def _merge_extractions(acc: dict, chunk: dict) -> dict:
        """Union the per-chunk extractions into one composite output.

        List fields union with normalized dedupe (case/whitespace-insensitive,
        so the overlap window re-quoting a clause is a no-op); scalar fields
        keep the FIRST non-null value in document order; confidence takes the
        MIN across the windows that produced fields (a document is only as
        reliable as its weakest productive window; a field-less window is
        ignored, see ``_window_confidence``; the failed-window cap is applied
        by ``finalize_chunked_merge``).
        ``reasoning`` is a TRACE: its entries union across chunks (dedupe by
        field, the first-witness evidence + section reference wins — the chunk
        that first located the value holds the evidence) and the summaries
        join with chunk markers, so the merged trace covers the whole
        document instead of only the first window.
        """
        # Decide the confidence BEFORE merging fields: the list union below
        # appends into ``acc``'s own lists (the copy is shallow), which would
        # make a field-less accumulator look populated by this very window.
        window_confidence = _window_confidence(acc, chunk) if "confidence" in chunk else None
        merged = dict(acc)
        for key, value in chunk.items():
            if key == "confidence":
                merged["confidence"] = window_confidence
                continue
            if key == "_parse_error":
                continue
            if key == "reasoning":
                merged["reasoning"] = _merge_reasoning(
                    merged.get("reasoning"), value)
                continue
            if isinstance(value, list):
                seen = {_norm(item) for item in merged.get(key) or []}
                for item in value:
                    if _norm(item) not in seen:
                        merged.setdefault(key, []).append(item)
                        seen.add(_norm(item))
            elif value not in (None, ""):
                # first NON-NULL value in document order wins (the accumulator
                # may hold a present-but-null key from an earlier chunk)
                if merged.get(key) in (None, ""):
                    merged[key] = value
        return merged

    def extract(
        self, doc_text: str, pages: list[str] | None = None  # MAILROOM PATCH: pages
    ) -> dict:
        self._last_chunked = False
        truncated = self.truncate_input(doc_text)
        # When the sorter hands this document off, its classification is
        # prefixed to the extraction call so the specialist extracts with the
        # expected clause set in mind (mailroom chained pipeline).
        user_message = f"Extract fields from this {self._doc_label} document:\n\n{truncated}"
        if self.handoff_context:
            user_message = (
                f"{self.handoff_context}\n\n"
                f"Extract fields from this {self._doc_label} document:\n\n{truncated}"
            )
        result = self._call_structured(
            user_message,
            json_schema=self.schema,
            temperature=0.1,
            pages=pages,  # MAILROOM PATCH
        )
        if result.get("_parse_error"):
            logger.error("specialist_parse_error", agent=self.agent_name)
            return {"_parse_error": True}
        if self._confidence_missing(result):
            # The model occasionally omits `confidence`; derive it from the
            # evidence in THIS document (the share of schema fields actually
            # found) — the rule the prompt itself states.
            result["confidence"] = round(self._evidence_confidence(result), 4)
        # Guarantee every schema field is present (null/[]/0.0 defaults).
        return normalize_extraction(result, self.schema)

    def _confidence_missing(self, result: dict) -> bool:
        value = result.get("confidence")
        return value is None or (isinstance(value, (int, float)) and value == 0.0)

    def _evidence_confidence(self, result: dict) -> float:
        """Share of schema fields actually found in the extraction (0.0-1.0).

        List fields count as found when non-empty; string fields when
        non-null. The confidence never exceeds what the extracted facts
        justify, mirroring the specialist prompts' evidence rule.
        """
        properties = (self.schema.get("properties") or {})
        total = 0
        found = 0
        for key, spec in properties.items():
            if key in ("confidence", "reasoning"):
                continue
            if key in ("cuad_family", "merger_consideration", "cuad_clauses", "maud_clauses"):
                # Absence is a valid inventory answer and must not drag confidence.
                continue
            value = result.get(key)
            type_spec = spec.get("type")
            if isinstance(type_spec, list):
                type_spec = next((t for t in type_spec if t != "null"), type_spec[0])
            total += 1
            if type_spec == "array":
                found += 1 if value not in (None, [], "") else 0
            else:
                found += 1 if value not in (None, "") else 0
        return found / total if total else 0.0

    @property
    def _doc_label(self) -> str:
        return self.agent_name.replace("_specialist", "").replace("_", " ")


class ContractsSpecialist(_SpecialistBase):
    agent_name = "contracts_specialist"
    schema = CONTRACTS_SCHEMA

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 prompt_version: str = "contracts_specialist", callbacks: list | None = None):
        super().__init__(model=model, api_key=api_key, callbacks=callbacks)
        self.prompt_version = prompt_version
        self._last_chunked = False
        self._last_n_chunks = 0

    def system_prompt(self) -> str:
        return get_prompt(self.prompt_version)


class CorporateRecordsSpecialist(_SpecialistBase):
    agent_name = "corporate_records_specialist"
    schema = CORPORATE_RECORDS_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("corporate_records_specialist")


class CorrespondenceSpecialist(_SpecialistBase):
    agent_name = "correspondence_specialist"
    schema = CORRESPONDENCE_SCHEMA

    def system_prompt(self) -> str:
        return get_prompt("correspondence_specialist")


class MergerAgreementSpecialist(_SpecialistBase):
    agent_name = "merger_agreement_specialist"
    schema = MERGER_AGREEMENT_SCHEMA

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 prompt_version: str = "merger_agreement_specialist",
                 callbacks: list | None = None):
        super().__init__(model=model, api_key=api_key, callbacks=callbacks)
        self.prompt_version = prompt_version
        self._last_chunked = False
        self._last_n_chunks = 0

    def system_prompt(self) -> str:
        return get_prompt(self.prompt_version)


# Specialist registry — maps doc_type keys to specialist classes
SPECIALIST_REGISTRY = {
    "contract": ContractsSpecialist,
    "merger_agreement": MergerAgreementSpecialist,
    "corporate_record": CorporateRecordsSpecialist,
    "correspondence": CorrespondenceSpecialist,
}


def get_specialist(doc_type: str, model: str | None = None, api_key: str | None = None) -> BaseAgent:
    """Get the specialist agent for a given document type.

    Args:
        doc_type: Document type key (e.g., "contract").
        model: Optional model override.
        api_key: Optional API key override.

    Returns:
        An instantiated specialist agent.

    Raises:
        ValueError: If no specialist exists for the doc_type.
    """
    resolved = doc_type
    try:
        from pipeline.config import resolve_extract_class

        resolved = resolve_extract_class(doc_type) or doc_type
    except Exception:
        resolved = doc_type
    if resolved not in SPECIALIST_REGISTRY:
        raise ValueError(f"No specialist registered for doc_type: {doc_type}")
    return SPECIALIST_REGISTRY[resolved](model=model, api_key=api_key)
