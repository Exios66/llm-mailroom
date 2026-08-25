from typing import TypedDict, Annotated, Any
from langgraph.graph.message import add_messages


class DocumentState(TypedDict, total=False):
    doc_id: str
    matter_id: str
    original_filename: str
    stage: str
    doc_type: str | None
    # Contract subtype (25 CUAD families + "other") from the LangChain sorter;
    # None for non-contract documents. Additive classification detail used for
    # the extraction handoff context and reporting.
    contract_subtype: str | None
    classification_confidence: float | None
    classification_attempts: int
    extracted_data: dict[str, Any] | None
    extraction_confidence: float | None
    extraction_attempts: int
    trace_id: str | None
    escalation_reason: str | None
    review_decision: str | None
    retry_count: int
    conflict_detected: bool
    conflict_details: list[str]
    extraction_guardrail: list[str]
    classification_guardrail: list[str]
    file_path: str
    doc_text: str
    # Page-image data-URIs rendered at ingest for vision-capable input agents
    # (PDFs rendered page-by-page; image files passed through). Sent to the
    # sorter/specialist prompts when the agent's model is vision-capable;
    # `doc_text` above is always produced regardless for text-only paths.
    doc_pages: list[str]
    # Intake clerk stats (procedural normalize at ingest; The-Mailroom reads
    # the matching ``normalize-intake`` span, not these state keys).
    intake_messy: bool
    intake_changed: bool
    error_message: str | None
    run_deadline: float
    run_aborted: bool
    # Transient provider-error retry (connection errors etc.): the node sets
    # `transient_error` and per-node `transient_retries_<node>` so routing
    # can retry the SAME node (self-loop) instead of consuming the
    # confidence-based retry budget. The generic `transient_retries` key is
    # unused; each node writes its own counter (L-13).
    transient_error: bool
    transient_retries: int
    # Attempt number of this pipeline run for a document (observability: trace
    # tags/metadata + seed suffix beyond the first run).
    run_attempt: int
    # KANBAN-062 (Lane A): independent second opinion from the sorter_reviewer
    # agent on medium-band classifications. The original sorter answer is
    # preserved untouched; `review_verdict` records the lane outcome
    # (reviewer_agrees | reviewer_overrides | escalated).
    reviewer_doc_type: str | None
    reviewer_contract_subtype: str | None
    reviewer_confidence: float | None
    review_verdict: str | None
    # KANBAN-063 (Lane B): in-pipeline judge verification + arbiter
    # arbitration. The judge fires only when gated in (needs_judge_review /
    # ambiguous-band extraction confidence); the arbiter only on a failed
    # verdict.
    judge_verdict: str | None
    judge_score: float | None
    judge_findings: list[str]
    arbiter_decision: str | None
    arbiter_reasoning: str | None
    arbiter_handoff: str | None
    # Named fields the arbiter asked the specialist to repair (Lane B retry).
    arbiter_fields_to_fix: list[str]
    # Bound on arbiter-driven re-extractions (mirrors the retry budget idea;
    # separate counter so extraction retries and arbitration retries never
    # alias each other).
    arbiter_retry_count: int
    messages: Annotated[list, add_messages]
