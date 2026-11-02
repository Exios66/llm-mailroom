"""Live LLM-Mailroom / The-Mailroom pipeline contract.

Pinned to llm-mailroom taxonomy after mailroom-issues #236–#238: five live
extraction classes (``contract``, ``merger_agreement``, ``corporate_record``,
``correspondence``, ``insurance_claim``), ``unknown`` routing token, no
merger→contract extract alias, Hub subclass inventories, CUAD/MAUD clause
fields, Langfuse data-model observation types, score transport aliases.

This module is organizational: it does not invent KPIs. Retired specialists
keep their scoring suites so historical traces and LegalBench still score;
they are flagged ``retired`` and never treated as live extract classes.
"""

from __future__ import annotations

from typing import Any, Iterable

from ._paired import require_equal_lengths

from .intake import (
    INTAKE_HANDOFF_NODE,
    INTAKE_LIVE_METHOD,
    INTAKE_METHODS,
    INTAKE_PREP_STEPS,
    INTAKE_SPAN,
    INTAKE_SPAN_KEYS,
)

__all__ = [
    "LIVE_DOC_TYPES",
    "RETIRED_DOC_TYPES",
    "UNKNOWN_DOC_TYPE",
    "EXTRACT_CLASS_ALIASES",
    "SORTER_LABEL_SET",
    "LIVE_SPECIALISTS",
    "RETIRED_SPECIALISTS",
    "RETIRED_AUDITORS",
    "INTAKE_AGENT",
    "INTAKE_SPAN",
    "INTAKE_HANDOFF_NODE",
    "INTAKE_METHODS",
    "INTAKE_LIVE_METHOD",
    "INTAKE_SPAN_KEYS",
    "INTAKE_PREP_STEPS",
    "PIPELINE_TRACE",
    "NODE_OBSERVATION_TYPES",
    "LANGFUSE_SCORE_NAME_ALIASES",
    "JUDGE_VERDICT_SCORE",
    "JUDGE_QUALITY_SCORE",
    "GROUND_TRUTH_KEYS",
    "HUB_SUBCLASS_INVENTORIES",
    "COMPLIANCE_FILING_TYPES",
    "INSURANCE_CLAIM_EXTRACT_TYPES",
    "CONTRACT_INVENTORY_FIELDS",
    "observation_type_for",
    "resolve_extract_class",
    "is_live_extract_class",
    "is_retired_doc_type",
    "langfuse_score_name",
    "canonical_score_name",
    "align_doc_type",
    "score_aligned_classification",
    "trace_identity",
]


#: Live taxonomy classes that dispatch to a specialist (mailroom-issues
#: #236–#238). ``merger_agreement`` is ``MergerAgreementExtraction``, not
#: an extract alias of ``contract``.
LIVE_DOC_TYPES: tuple[str, ...] = (
    "contract",
    "merger_agreement",
    "corporate_record",
    "correspondence",
    "insurance_claim",
)

#: Retired from the live pipeline. Sorter emits ``unknown``; scoring
#: suites remain for historical traces / LegalBench. ``compliance_filing``
#: left the live extract roster with the mailroom 2026-09-15 cut.
RETIRED_DOC_TYPES: tuple[str, ...] = (
    "court_opinion",
    "due_diligence",
    "compliance_filing",
)

#: Routing token — not a taxonomy class and not a specialist.
UNKNOWN_DOC_TYPE = "unknown"

#: Sorter / HF labels that extract through a live specialist without a
#: new taxonomy row. Empty: ``merger_agreement`` is a live MAUD class,
#: not an alias of CUAD ``contract`` (mailroom-issues #237 / #238).
EXTRACT_CLASS_ALIASES: dict[str, str] = {}

#: Labels the sorter / Lane A reviewer may emit.
SORTER_LABEL_SET: tuple[str, ...] = LIVE_DOC_TYPES + (UNKNOWN_DOC_TYPE,)

LIVE_SPECIALISTS: tuple[str, ...] = (
    "contracts_specialist",
    "merger_agreement_specialist",
    "corporate_records_specialist",
    "correspondence_specialist",
    "insurance_claims_specialist",
)

RETIRED_SPECIALISTS: tuple[str, ...] = (
    "court_opinions_specialist",
    "due_diligence_specialist",
    "compliance_specialist",
)

RETIRED_AUDITORS: tuple[str, ...] = (
    "court_opinions_auditor",
    "due_diligence_auditor",
    "compliance_auditor",
)

INTAKE_AGENT = "intake"

#: Production pipeline trace name (The-Mailroom default filter).
PIPELINE_TRACE = "document-pipeline"

#: Graph node → Langfuse observation type (llm-mailroom ``tracing.py``).
NODE_OBSERVATION_TYPES: dict[str, str] = {
    "document-pipeline": "chain",
    "intake-document": "span",
    "normalize-intake": "span",
    "extract-image-text": "retriever",
    "transcribe-pdf": "retriever",
    "classify-document": "agent",
    "extract-fields": "agent",
    "judge-verify": "evaluator",
    "arbitrate-verdict": "agent",
    "arbitrate-classification": "agent",
    "route-for-review": "span",
    "adjudicate-conflict": "agent",
    "compile-report": "agent",
    "write-catalog": "span",
    "archive-document": "span",
    "pipeline-result": "generation",
    "answer-question": "generation",
}

#: Langfuse Cloud rejects score *config* names over 35 characters.
#: Canonical Python/dojo name stays long; the wire alias is shorter.
LANGFUSE_SCORE_NAME_ALIASES: dict[str, str] = {
    "extraction_overall_verified_precision": "extraction_verified_precision",
}

JUDGE_VERDICT_SCORE = "mailroom-pipeline-judge"
JUDGE_QUALITY_SCORE = "mailroom-pipeline-quality"

#: Ground-truth keys The-Mailroom / HF pilot put on trace input/metadata.
#: ``expected_fields`` stays OFF the trace (mailroom ``_public_ground_truth``).
GROUND_TRUTH_KEYS: tuple[str, ...] = (
    "expected_hf_class",
    "expected_doc_class",
    "expected_subclass",
    "expected",
)

#: Hub ``expected_subclass`` inventories (llm-mailroom ``doc_inventories.py``).
HUB_SUBCLASS_INVENTORIES: dict[str, tuple[str, ...]] = {
    "corporate_record": (
        "articles_of_incorporation",
        "board_resolution",
        "bylaws",
        "charter_amendment",
        "indenture",
        "officer_certificate",
        "other",
        "powers_of_attorney",
        "rights_instrument",
        "subsidiary_list",
    ),
    "correspondence": (
        "email",
        "letter",
        "memo",
        "notice",
        "demand",
        "attorney_demand",
        "press_release",
        "meeting_request",
    ),
    "insurance_claim": (
        "pde",
        "inpatient",
        "outpatient",
        "carrier",
        "property",
        "auto",
    ),
    "compliance_filing": (
        "10-K",
        "10-Q",
        "8-K",
        "S-1",
        "DEF 14A",
        "13D",
        "13G",
        "Form 4",
        "20-F",
        "6-K",
        "other",
    ),
}

COMPLIANCE_FILING_TYPES: tuple[str, ...] = HUB_SUBCLASS_INVENTORIES["compliance_filing"]

#: Extraction ``claim_type`` enum: CMS source tables first, then legacy FNOL.
INSURANCE_CLAIM_EXTRACT_TYPES: tuple[str, ...] = (
    "pde",
    "inpatient",
    "outpatient",
    "carrier",
    "auto",
    "property",
    "liability",
    "health",
    "life",
    "workers_comp",
    "other",
)

#: Extra ContractExtraction fields (CUAD/MAUD Hub inventories). ``reasoning``
#: is a TRACE artifact and is never scored.
CONTRACT_INVENTORY_FIELDS: dict[str, str] = {
    "cuad_family": "name",
    "merger_consideration": "name",
    "cuad_clauses": "entity_list:free_text",
    "maud_clauses": "entity_list:free_text",
}


def observation_type_for(name: str, default: str = "span") -> str:
    """Langfuse observation type for a graph-node / span name."""
    return NODE_OBSERVATION_TYPES.get(name, default)


def resolve_extract_class(doc_type: str | None) -> str | None:
    """Live taxonomy class used for extraction, or ``None`` if parked."""
    if not doc_type:
        return None
    key = str(doc_type).strip().lower()
    if key == UNKNOWN_DOC_TYPE or key in RETIRED_DOC_TYPES:
        return None
    aliased = EXTRACT_CLASS_ALIASES.get(key, key)
    if aliased in LIVE_DOC_TYPES:
        return aliased
    return None


def is_live_extract_class(doc_type: str | None) -> bool:
    """Return whether a document type resolves to a live extraction class."""
    return resolve_extract_class(doc_type) is not None


def is_retired_doc_type(doc_type: str | None) -> bool:
    """Check whether a normalized document type is explicitly retired."""
    return (doc_type or "").strip().lower() in RETIRED_DOC_TYPES


def langfuse_score_name(name: str) -> str:
    """Name actually sent to Langfuse (may be a short transport alias)."""
    return LANGFUSE_SCORE_NAME_ALIASES.get(name, name)


def canonical_score_name(name: str) -> str:
    """Inverse of :func:`langfuse_score_name` for scores read back from Langfuse."""
    for canonical, alias in LANGFUSE_SCORE_NAME_ALIASES.items():
        if name == alias:
            return canonical
    return name


def align_doc_type(value: Any) -> str:
    """HF aligned label. Extract aliases resolve; merger stays merger."""
    key = str(value or "").strip().lower()
    return EXTRACT_CLASS_ALIASES.get(key, key)


def score_aligned_classification(
    expected: Iterable,
    predicted: Iterable,
) -> dict[str, float | int]:
    """Exact vs aligned doc-type accuracy (The-Mailroom ``eval_pipeline``).

    Aligned applies :data:`EXTRACT_CLASS_ALIASES` only. ``merger_agreement``
    is not equivalent to ``contract`` — predicting ``contract`` for a MAUD
    document is a miss. ``unknown`` and retired types stay distinct.
    """
    expected, predicted = list(expected), list(predicted)
    require_equal_lengths(expected, predicted)
    pairs = list(zip(expected, predicted))
    n = len(pairs)
    if not n:
        return {
            "n": 0,
            "exact_accuracy": 0.0,
            "aligned_accuracy": 0.0,
            "n_exact": 0,
            "n_aligned": 0,
        }
    n_exact = sum(1 for e, p in pairs if str(e).strip().lower() == str(p).strip().lower())
    n_aligned = sum(1 for e, p in pairs if align_doc_type(e) == align_doc_type(p))
    return {
        "n": n,
        "exact_accuracy": round(n_exact / n, 4),
        "aligned_accuracy": round(n_aligned / n, 4),
        "n_exact": n_exact,
        "n_aligned": n_aligned,
    }


def trace_identity(trace: dict[str, Any]) -> dict[str, Any]:
    """Pull producer identity fields (v3 snake_case and v4 camelCase)."""
    meta = trace.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    def _both(*keys: str) -> Any:
        """Find the first nonempty key value, checking trace fields before metadata per key."""
        for key in keys:
            if trace.get(key) not in (None, ""):
                return trace[key]
            if meta.get(key) not in (None, ""):
                return meta[key]
        return None

    return {
        "trace_id": _both("id", "trace_id", "traceId"),
        "session_id": _both("session_id", "sessionId"),
        "user_id": _both("user_id", "userId"),
        "release": _both("release", "version", "langfuse_release"),
        "environment": _both("environment"),
        "name": _both("name"),
        "tags": trace.get("tags") or meta.get("tags") or [],
    }
